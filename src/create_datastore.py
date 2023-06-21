# Some code in the initializer is taken from tutorials of the FAISS library. Taken from
# https://github.com/facebookresearch/faiss
# The code contains the class of a datastore, where the keys are the hidden states and values are target next tokens.
# Modified from: https://raw.githubusercontent.com/tongyao-zhu/knn-mt-reimplement/main/datastore.py

import argparse
import logging
import os
import time
from typing import Tuple

import faiss  # make faiss available
import faiss.contrib.torch_utils
import numpy as np
import torch
import tqdm

from constants import (
    INDEX_FILE,
    INPUT_ID_FILE,
    RAW_FEATURE_KEY_SUFFIX,
    RAW_FEATURE_TOKEN_SUFFIX,
    RAW_FEATURE_VALUE_SUFFIX,
    TOKEN_ID_FILE,
)
from utils.get_projection_matrix import transform_and_normalize, whitening

# set up logger
logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)


class DataStore:
    """
    This class represents a datastore. It can be trained from raw features, saved and loaded from disk.
    During inference time, it can search given a query and return the normalized score for each token.
    """

    def __init__(self, d=768, args=None):
        """
        Set the necessary attributes. The number follow the original paper.
        """
        co = faiss.GpuClonerOptions()
        # co.useFloat16 = True  # to avoid GPU memory issue
        resources = faiss.StandardGpuResources()
        self.d = d  # dimension of keys #1024 is from paper
        n_centroids = 4096  # number of clustering centroids to learn # 4096 was original

        metric = faiss.METRIC_L2
        quantizer = faiss.IndexFlatL2(self.d)
        index = faiss.IndexIVFFlat(quantizer, d, n_centroids, metric)
        index = faiss.index_cpu_to_gpu(resources, 0, index, co)
        index.nprobe = 32  # number of clusters to query, 32 was original
        self.vocab_size = -1  # to be set later
        self.index = index

        if args:
            self.args = args

    def load(self, saved_dir: str) -> None:
        """
        Load the pretrained FAISS index from a directory with the necessary components. The directory should include:
        - index.trained : the trained index
        - token_ids.pt : the label ids sorted by their index id in FAISS.
        - input_ids.pt : the token ids sorted by their index id in FAISS.
        :param saved_dir: The directory containing the trained index.
        :return: None. The attributes of this Datastore instance will be set.
        """
        logger.info(
            f"Started loading trained index and token ids lookup from {saved_dir}"
        )
        self.index = faiss.read_index(os.path.join(saved_dir, INDEX_FILE))
        self.label_id_store = torch.tensor(
            torch.load(os.path.join(saved_dir, TOKEN_ID_FILE))
        )
        self.input_id_store = torch.tensor(
            torch.load(os.path.join(saved_dir, INPUT_ID_FILE))
        )
        logger.info(f"Finished loading trained index and token ids from {saved_dir}")

    def train_index(self, key_store: np.ndarray) -> None:
        """
        Training the FAISS index. We will perform random sampling on the keys.
        :param key_store: a numpy array with shape (num_keys, dim_keys), each row is a key
        :return: None. The index attribute will be updated after training.
        """
        logger.info(f"Start training the index, this might take a long time.")
        start = time.time()
        self.index.train(key_store)
        logger.info(
            f"Finished training the index. It took {(time.time() - start)} seconds."
        )
        self.index = faiss.index_gpu_to_cpu(self.index)  # put back to CPU

    def read_feature_files(self, feature_dir: str, percentage: int = 100) -> Tuple:
        """
        Read the raw features generated by generate_raw_feature.py, and stack them into on single tensor.
        :param feature_dir: The directory containing the raw features.
        :param percentage: The percentage of files to read from (mainly for testing purpose).
        :return:
        key_store: a numpy array of shape (num_keys, dim_keys), each row is a key
        label_id_store: a numpy array of shape (num_keys, 1), each row represents the value (target token) to the key.
        """
        value_files = list(
            filter(
                lambda x: x.endswith(RAW_FEATURE_VALUE_SUFFIX), os.listdir(feature_dir)
            )
        )
        value_files = value_files[: int(len(value_files) * (percentage / 100.0))]
        key_store = []
        label_id_store = []
        input_id_store = []
        start_time = time.time()
        for file_name in tqdm.tqdm(
            value_files, total=len(value_files), desc="Loading feature files"
        ):
            file_id = file_name.split(RAW_FEATURE_VALUE_SUFFIX)[0]
            key_path = os.path.join(feature_dir, str(file_id) + RAW_FEATURE_KEY_SUFFIX)
            value_path = os.path.join(
                feature_dir, str(file_id) + RAW_FEATURE_VALUE_SUFFIX
            )
            input_id_path = os.path.join(
                feature_dir, str(file_id) + RAW_FEATURE_TOKEN_SUFFIX
            )
            try:
                curr_keys = torch.load(key_path)
                curr_label_ids = torch.load(value_path)
                curr_input_ids = torch.load(input_id_path)
            except Exception as e:
                logger.error(f"Failed to load {key_path} or {value_path}.")
                raise IOError(e)
            key_store += (
                curr_keys.cpu()
            )  # ensure that it is on CPU, as numpy doesn't support GPU
            label_id_store += curr_label_ids.cpu()
            input_id_store += curr_input_ids.cpu()
        key_store = np.stack(key_store)
        label_id_store = np.stack(label_id_store)
        input_id_store = np.stack(input_id_store)

        logger.info(
            f"{len(key_store)} keys and values, used {time.time() - start_time} seconds"
        )
        return key_store, label_id_store, input_id_store

    def read_features_and_train(
        self, feature_dir: str, output_dir: str, percentage: int = 100
    ) -> None:
        """
        Read features and train the index. The result will be saved.
        :param feature_dir: The directory containing the raw features from generate_raw_features.py
        :param output_dir: The output directory to save the trained index and index-to-token mapping.
        :param percentage: The percentage of the all features to perform training
        :return: None. The trained index will be saved to output_dir.
        """
        key_store, label_id_store, input_id_store = self.read_feature_files(
            feature_dir=feature_dir, percentage=percentage
        )
        self.label_id_store = torch.tensor(label_id_store)
        self.input_id_store = torch.tensor(input_id_store)

        if self.args.whitening:
            logger.info("****** Getting Projection ******")
            kernel, bias = whitening(key_store)

            if self.args.dim_reduction:
                kernel = kernel[:, : self.d]

            logger.info(f"Saving Projection Matrix in {output_dir}")
            torch.save(kernel, f"{output_dir}/kernel.pt")
            torch.save(bias, f"{output_dir}/bias.pt")

            key_store = transform_and_normalize(key_store, kernel, bias)
            # print(key_store.size())

        self.train_index(key_store)
        self.add_keys(key_store)
        self.save(output_dir)
        return

    def add_keys(self, keys_to_add: np.ndarray) -> None:
        """
        Add the keys to the trained index.
        :param keys_to_add: a numpy array of shape (num_keys, keys_dim)
        :return: The index will be updated with the input keys.
        """
        logger.info("Start adding keys to the index.")
        start_time = time.time()
        self.index.add(keys_to_add)  # add vectors to the index
        logger.info(
            f"Finished adding keys to the index. It took {time.time() - start_time} seconds"
        )

    def save(self, output_dir: str) -> None:
        """
        Save the index and the index-to-token mapping in the output_dir.
        :param output_dir: The directory to save the results.
        :return: None. Results will be saved to output_dir.
        """
        try:
            # write the trained index
            faiss.write_index(self.index, os.path.join(output_dir, INDEX_FILE))
        except Exception as e:
            logger.error(f"Encountered error when writing FAISS index to {output_dir}")
            raise IOError(e)

        try:
            # save the index for token_ids
            torch.save(self.label_id_store, os.path.join(output_dir, TOKEN_ID_FILE))
            # save the index for input_ids
            torch.save(self.input_id_store, os.path.join(output_dir, INPUT_ID_FILE))
        except Exception as e:
            logger.error(f"Encountered error when saving torch tensor to {output_dir}")
            raise IOError(e)
        logger.info(
            f"Successfully saved the trained index ({INDEX_FILE}, and {TOKEN_ID_FILE}) to {output_dir}"
        )

    def set_vocab_size(self, vocab_size: int) -> None:
        """
        Set the vocabulary size of the datastore. This will be used when generating score tensors for each token. For
        classification this will be equal to the size of the label space.
        :param vocab_size: size of the vocab of the language model.
        :return: None. the attribute will be set
        """
        self.vocab_size = vocab_size

    def search_k(self, query: torch.tensor, k: int, T: float) -> torch.tensor:
        """
        Search for the top K nearest neighbors, along with the distance.
        :param T: temperature
        :param k: top k
        :param query: should have shape (num_queries, dim_keys).
        :return: scores: should have shape (num_queries, vocab_size), contains scores for each token for each entry
        """
        assert (
            self.vocab_size >= 1
        ), "Please set the vocab size first (using set_vocab_size method) before the search!"
        # faiss.normalize_L2(query)
        D, I = self.index.search(
            query, k
        )  # D, I will have shape (num_queries, k), containing the distance and the index
        actual_label_ids = self.label_id_store[torch.tensor(I)]  # (num_queries, k)
        actual_input_ids = self.input_id_store[torch.tensor(I)]  # (num_queries, k)
        scores = torch.zeros((query.shape[0], self.vocab_size))
        distance_scores = torch.softmax(
            -torch.tensor(D) / T, dim=-1
        )  # softmax of the distance
        scores = scores.scatter(
            1, actual_label_ids, distance_scores, reduce="add"
        )  # will assign the scores to indices and aggregate for each token
        return scores, actual_input_ids, I


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate raw feature tensors for building the datastore"
    )
    parser.add_argument(
        "--feature_dir",
        type=str,
        required=True,
        help="the directory of the generated raw features",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="the directory to save the trained index files",
    )
    parser.add_argument(
        "--sample_percentage",
        type=int,
        default=100,  # by default, use all available data
        help="The percentage to use for training",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,  # by default, use all available data
        help="Set a seed for reproducibility",
    )
    parser.add_argument(
        "--whitening",
        action="store_true",
        help="use whitening before storing keys (Su et al., 2021)",
    )
    parser.add_argument(
        "--dim_reduction",
        type=int,
        default=768,
        help="use whitening and dim reduction before storing keys (Su et al., 2021)",
    )
    args = parser.parse_args()

    return args


def read_and_train():
    """
    Read the raw features and train the index.
    :return:
    """
    if not (torch.cuda.is_available()):
        logger.warning(
            "No GPU detected in the environment. Not training on GPU can be very slow"
        )

    args = parse_args()
    np.random.seed(args.seed)
    datastore = DataStore(d=args.dim_reduction, args=args)

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
        print(f"Folder '{args.output_dir}' created successfully.")
    else:
        print(f"Folder '{args.output_dir}' already exists.")

    datastore.read_features_and_train(
        feature_dir=args.feature_dir,
        output_dir=args.output_dir,
        percentage=args.sample_percentage,
    )


if __name__ == "__main__":
    read_and_train()
