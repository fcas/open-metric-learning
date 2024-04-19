from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import albumentations as albu
import numpy as np
import pandas as pd
import torch
import torchvision
from torch import BoolTensor, FloatTensor, LongTensor

from oml.const import (
    BLACK,
    CATEGORIES_COLUMN,
    CATEGORIES_KEY,
    INDEX_KEY,
    INPUT_TENSORS_KEY,
    IS_GALLERY_COLUMN,
    IS_GALLERY_KEY,
    IS_QUERY_COLUMN,
    IS_QUERY_KEY,
    LABELS_COLUMN,
    LABELS_KEY,
    PATHS_COLUMN,
    PATHS_KEY,
    SEQUENCE_COLUMN,
    SEQUENCE_KEY,
    SPLIT_COLUMN,
    X1_COLUMN,
    X1_KEY,
    X2_COLUMN,
    X2_KEY,
    Y1_COLUMN,
    Y1_KEY,
    Y2_COLUMN,
    Y2_KEY,
    TBBoxes,
    TColor,
)
from oml.interfaces.datasets import (
    IBaseDataset,
    IDatasetLabeled,
    IDatasetQueryGalleryLabeled,
    IVisualizableDataset,
)
from oml.registry.transforms import get_transforms
from oml.transforms.images.utils import TTransforms, get_im_reader_for_transforms
from oml.utils.dataframe_format import check_retrieval_dataframe_format
from oml.utils.images.images import TImReader, get_img_with_bbox, square_pad

# todo 522: general comment on Datasets
# We will remove using keys in __getitem__ for:
# Passing extra information (like categories or sequence id) -> we will use .extra_data instead
# Modality related info (like bboxes or paths) -> they may only exist as internals of the datasets
# is_query_key, is_gallery_key -> get_query_ids() and get_gallery_ids() methods
# Before this, we temporary keep both approaches


def parse_bboxes(df: pd.DataFrame) -> Optional[TBBoxes]:
    n_existing_columns = sum([x in df for x in [X1_COLUMN, X2_COLUMN, Y1_COLUMN, Y2_COLUMN]])

    if n_existing_columns == 4:
        bboxes = []
        for row in df.iterrows():
            bbox = int(row[X1_COLUMN]), int(row[X2_COLUMN]), int(row[Y1_COLUMN]), int(row[Y2_COLUMN])
            bbox = None if any(coord is None for coord in bbox) else bbox
            bboxes.append(bbox)

    elif n_existing_columns == 0:
        bboxes = None

    else:
        raise ValueError(f"Found {n_existing_columns} bounding bboxes columns instead of 4. Check your dataframe.")

    return bboxes


class ImagesBaseDataset(IBaseDataset, IVisualizableDataset):
    """
    The base class that handles image specific logic.

    """

    input_tensors_key: str
    index_key: str

    def __init__(
        self,
        paths: List[Path],
        dataset_root: Optional[Union[str, Path]] = None,
        bboxes: Optional[TBBoxes] = None,
        extra_data: Optional[Dict[str, Any]] = None,
        transform: Optional[TTransforms] = None,
        f_imread: Optional[TImReader] = None,
        cache_size: Optional[int] = 0,
        input_tensors_key: str = INPUT_TENSORS_KEY,
        index_key: str = INDEX_KEY,
        # todo 522: remove
        paths_key: str = PATHS_KEY,
        x1_key: str = X1_KEY,
        x2_key: str = X2_KEY,
        y1_key: str = Y1_KEY,
        y2_key: str = Y2_KEY,
    ):
        """

        Args:
            paths: Paths to images. Will be concatenated with ``dataset_root`` is provided.
            dataset_root: Path to the images' dir, set ``None`` if you provided the absolute paths in your dataframe
            bboxes: Bounding boxes of images. Some of the images may not have bounding bboxes.
            extra_data: Dictionary containing records of some additional information.
            transform: Augmentations for the images, set ``None`` to perform only normalisation and casting to tensor
            f_imread: Function to read the images, pass ``None`` to pick it automatically based on provided transforms
            cache_size: Size of the dataset's cache
            input_tensors_key: Key to put tensors into the batches
            index_key: Key to put samples' ids into the batches
            paths_key: Key put paths into the batches   # todo 522: remove
            x1_key: Key to put ``x1`` into the batches  # todo 522: remove
            x2_key: Key to put ``x2`` into the batches  # todo 522: remove
            y1_key: Key to put ``y1`` into the batches  # todo 522: remove
            y2_key: Key to put ``y2`` into the batches  # todo 522: remove

        """
        assert (bboxes is None) or (len(paths) == len(bboxes))

        if extra_data is not None:
            assert all(
                len(record) == len(paths) for record in extra_data.values()
            ), "All the extra records need to have the size equal to the dataset's size"

        self.input_tensors_key = input_tensors_key
        self.index_key = index_key

        if dataset_root is not None:
            self._paths = list(map(lambda x: str(Path(dataset_root) / x), paths))
        else:
            self._paths = list(map(str, paths))

        self.extra_data = extra_data

        self._bboxes = bboxes
        self._transform = transform if transform else get_transforms("norm_albu")
        self._f_imread = f_imread or get_im_reader_for_transforms(transform)

        if cache_size:
            self.read_bytes = lru_cache(maxsize=cache_size)(self._read_bytes)  # type: ignore
        else:
            self.read_bytes = self._read_bytes  # type: ignore

        available_transforms = (albu.Compose, torchvision.transforms.Compose)
        assert isinstance(self._transform, available_transforms), f"Transforms must one of: {available_transforms}"

        # todo 522: remove
        self.paths_key = paths_key
        self.x1_key = x1_key
        self.x2_key = x2_key
        self.y1_key = y1_key
        self.y2_key = y2_key

    @staticmethod
    def _read_bytes(path: Union[Path, str]) -> bytes:
        with open(str(path), "rb") as fin:
            return fin.read()

    def __getitem__(self, idx: int) -> Dict[str, Union[FloatTensor, int]]:
        img_bytes = self.read_bytes(self._paths[idx])
        img = self._f_imread(img_bytes)

        im_h, im_w = img.shape[:2] if isinstance(img, np.ndarray) else img.size[::-1]

        if (self._bboxes is not None) and (self._bboxes[idx] is not None):
            x1, y1, x2, y2 = self._bboxes[idx]
        else:
            x1, y1, x2, y2 = 0, 0, im_w, im_h

        if isinstance(self._transform, albu.Compose):
            img = img[y1:y2, x1:x2, :]
            image_tensor = self._transform(image=img)["image"]
        else:
            # torchvision.transforms
            img = img.crop((x1, y1, x2, y2))
            image_tensor = self._transform(img)

        item = {
            self.input_tensors_key: image_tensor,
            self.index_key: idx,
        }

        if self.extra_data:
            for key, record in self.extra_data.items():
                if key in item:
                    raise ValueError(f"<extra_data> and dataset share the same key: {key}")
                else:
                    item[key] = record[idx]

        # todo 522: remove
        item[self.x1_key] = x1
        item[self.y1_key] = y1
        item[self.x2_key] = x2
        item[self.y2_key] = y2
        item[self.paths_key] = self._paths[idx]

        return item

    def __len__(self) -> int:
        return len(self._paths)

    def visualize(self, idx: int, color: TColor = BLACK) -> np.ndarray:
        bbox = torch.tensor(self._bboxes[idx]) if (self._bboxes is not None) else torch.tensor([None] * 4)
        image = get_img_with_bbox(im_path=self._paths[idx], bbox=bbox, color=color)
        image = square_pad(image)

        return image

    # todo 522: remove
    @property
    def bboxes_keys(self) -> Tuple[str, ...]:
        return self.x1_key, self.y1_key, self.x2_key, self.y2_key


class ImagesDatasetLabeled(ImagesBaseDataset, IDatasetLabeled):
    """
    The dataset of images having their ground truth labels.

    """

    def __init__(
        self,
        df: pd.DataFrame,
        extra_data: Optional[Dict[str, Any]] = None,
        dataset_root: Optional[Union[str, Path]] = None,
        transform: Optional[albu.Compose] = None,
        f_imread: Optional[TImReader] = None,
        cache_size: Optional[int] = 0,
        input_tensors_key: str = INPUT_TENSORS_KEY,
        labels_key: str = LABELS_KEY,
        index_key: str = INDEX_KEY,
        # todo 522: remove
        paths_key: str = PATHS_KEY,
        categories_key: Optional[str] = CATEGORIES_KEY,
        sequence_key: Optional[str] = SEQUENCE_KEY,
        x1_key: str = X1_KEY,
        x2_key: str = X2_KEY,
        y1_key: str = Y1_KEY,
        y2_key: str = Y2_KEY,
    ):
        assert (LABELS_COLUMN in df) and (PATHS_COLUMN in df), "There are only 2 required columns."
        self.labels_key = labels_key
        self.df = df

        extra_data = {} if extra_data is None else extra_data

        super().__init__(
            paths=self.df[PATHS_COLUMN].tolist(),
            bboxes=parse_bboxes(self.df),
            extra_data=extra_data,
            dataset_root=dataset_root,
            transform=transform,
            f_imread=f_imread,
            cache_size=cache_size,
            input_tensors_key=input_tensors_key,
            index_key=index_key,
            # todo 522: remove
            x1_key=x1_key,
            y2_key=y2_key,
            x2_key=x2_key,
            y1_key=y1_key,
            paths_key=paths_key,
        )

        # todo 522: remove
        self.categories_key = categories_key if (CATEGORIES_COLUMN in df.columns) else None
        self.sequence_key = sequence_key if (SEQUENCE_COLUMN in df.columns) else None

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        item = super().__getitem__(idx)
        item[self.labels_key] = self.df.iloc[idx][LABELS_COLUMN]

        # todo 522: remove
        if self.sequence_key:
            item[self.sequence_key] = self.df[SEQUENCE_COLUMN][idx]

        if self.categories_key:
            item[self.categories_key] = self.df[CATEGORIES_COLUMN][idx]

        return item

    def get_labels(self) -> np.ndarray:
        return np.array(self.df[LABELS_COLUMN])

    # todo 522: remove
    def get_label2category(self) -> Optional[Dict[int, Union[str, int]]]:
        if CATEGORIES_COLUMN in self.df.columns:
            label2category = dict(zip(self.df[LABELS_COLUMN], self.df[CATEGORIES_COLUMN]))
        else:
            label2category = None

        return label2category


class ImagesDatasetQueryGalleryLabeled(ImagesDatasetLabeled, IDatasetQueryGalleryLabeled):
    """
    The dataset of images having `query`/`gallery` split.

    Note, that some datasets used as benchmarks in Metric Learning
    explicitly provide the splitting information (for example, ``DeepFashion InShop`` dataset), but some of them
    don't (for example, ``CARS196`` or ``CUB200``). The validation idea for the latter is to perform `1 vs rest`
    validation, where every query is evaluated versus the whole validation dataset (except for this exact query).

    So, if you want an item participate in validation as both: query and gallery, you should mark this item as
    ``is_query == True`` and ``is_gallery == True``, as it's done in the `CARS196` or `CUB200` dataset.

    """

    def __init__(
        self,
        df: pd.DataFrame,
        extra_data: Optional[Dict[str, Any]] = None,
        dataset_root: Optional[Union[str, Path]] = None,
        transform: Optional[albu.Compose] = None,
        f_imread: Optional[TImReader] = None,
        cache_size: Optional[int] = 0,
        input_tensors_key: str = INPUT_TENSORS_KEY,
        labels_key: str = LABELS_KEY,
        # todo 522: remove
        paths_key: str = PATHS_KEY,
        categories_key: Optional[str] = CATEGORIES_KEY,
        sequence_key: Optional[str] = SEQUENCE_KEY,
        x1_key: str = X1_KEY,
        x2_key: str = X2_KEY,
        y1_key: str = Y1_KEY,
        y2_key: str = Y2_KEY,
        is_query_key: str = IS_QUERY_KEY,
        is_gallery_key: str = IS_GALLERY_KEY,
    ):
        assert all(x in df.columns for x in (IS_QUERY_COLUMN, IS_GALLERY_COLUMN, LABELS_COLUMN))
        self._df = df

        super().__init__(
            df=df,
            extra_data=extra_data,
            dataset_root=dataset_root,
            transform=transform,
            f_imread=f_imread,
            cache_size=cache_size,
            input_tensors_key=input_tensors_key,
            labels_key=labels_key,
            # todo 522: remove
            x1_key=x1_key,
            y2_key=y2_key,
            x2_key=x2_key,
            y1_key=y1_key,
            paths_key=paths_key,
            categories_key=categories_key,
            sequence_key=sequence_key,
        )

        # todo 522: remove
        self.is_query_key = is_query_key
        self.is_gallery_key = is_gallery_key

    def get_query_ids(self) -> LongTensor:
        return BoolTensor(self._df[IS_QUERY_COLUMN]).nonzero().squeeze()

    def get_gallery_ids(self) -> LongTensor:
        return BoolTensor(self._df[IS_GALLERY_COLUMN]).nonzero().squeeze()

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        item = super().__getitem__(idx)
        item[self.labels_key] = self._df.iloc[idx][LABELS_COLUMN]

        # todo 522: remove
        item[self.is_query_key] = bool(self._df[IS_QUERY_COLUMN][idx])
        item[self.is_gallery_key] = bool(self._df[IS_GALLERY_COLUMN][idx])

        return item


def get_retrieval_images_datasets(
    dataset_root: Path,
    transforms_train: Any,
    transforms_val: Any,
    f_imread_train: Optional[TImReader] = None,
    f_imread_val: Optional[TImReader] = None,
    dataframe_name: str = "df.csv",
    cache_size: Optional[int] = 0,
    verbose: bool = True,
) -> Tuple[IDatasetLabeled, IDatasetQueryGalleryLabeled]:
    df = pd.read_csv(dataset_root / dataframe_name, index_col=False)

    check_retrieval_dataframe_format(df, dataset_root=dataset_root, verbose=verbose)

    # todo 522: why do we need it?
    # first half will consist of "train" split, second one of "val"
    # so labels in train will be from 0 to N-1 and labels in test will be from N to K
    mapper = {l: i for i, l in enumerate(df.sort_values(by=[SPLIT_COLUMN])[LABELS_COLUMN].unique())}

    # train
    df_train = df[df[SPLIT_COLUMN] == "train"].reset_index(drop=True)
    df_train[LABELS_COLUMN] = df_train[LABELS_COLUMN].map(mapper)

    train_dataset = ImagesDatasetLabeled(
        df=df_train,
        dataset_root=dataset_root,
        transform=transforms_train,
        cache_size=cache_size,
        f_imread=f_imread_train,
    )

    # val (query + gallery)
    df_query_gallery = df[df[SPLIT_COLUMN] == "validation"].reset_index(drop=True)
    valid_dataset = ImagesDatasetQueryGalleryLabeled(
        df=df_query_gallery,
        dataset_root=dataset_root,
        transform=transforms_val,
        cache_size=cache_size,
        f_imread=f_imread_val,
    )

    return train_dataset, valid_dataset


__all__ = [
    "ImagesBaseDataset",
    "ImagesDatasetLabeled",
    "ImagesDatasetQueryGalleryLabeled",
    "get_retrieval_images_datasets",
]