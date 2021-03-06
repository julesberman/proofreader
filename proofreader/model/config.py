import numpy as np
import pprint
from typing import Tuple
from dataclasses import dataclass

import torch
import torch.nn.functional as F
from torch.optim.lr_scheduler import CosineAnnealingLR

from proofreader.data.augment import Augmentor
from proofreader.model.pointnet import PointNet
from proofreader.model.curvenet import CurveNet
from proofreader.model.transnet import PointTransformerCls
from proofreader.utils.torch import DatasetWithInfo


@dataclass
class AugmentorConfig:
    shuffle: bool = True
    center: bool = True
    random_scale: bool = False
    normalize: tuple = (125, 1250, 1250)
    num_points: int = None


@dataclass
class DatasetConfig:
    dataset: str = 'slice'
    num_slices = 2
    radius: int = 96
    context_slices: int = 3
    num_points: int = 2048
    val_split: float = 0.15
    verbose: bool = False
    path: str = None
    truncate_canidates: int = 4
    scale: bool = False
    balance_samples: bool = False


@dataclass
class ModelConfig:
    model: str = 'curvenet'
    loss: str = 'bce'
    optimizer: str = 'AdamW'
    scheduler: str = 'cos'
    learning_rate: float = 1e-3
    k: int = 20
    num_classes: int = 2
    loss_weights: Tuple = (1.0, 1.0)


@dataclass
class ExperimentConfig:
    name: str
    dataset: DatasetConfig = DatasetConfig()
    model: ModelConfig = ModelConfig()
    augmentor: AugmentorConfig = AugmentorConfig()

    def toString(self):
        d = pprint.pformat(self.dataset)
        m = pprint.pformat(self.model)
        return f'NAME\n{self.name}\nDATASET{d}\nMODEL\n{m}\n'


def load_dataset_from_disk(dataset_config, aug_config, test=False):

    # build augmentor
    train_augmentor = Augmentor(center=aug_config.center, shuffle=aug_config.shuffle, normalize=aug_config.normalize, num_points=dataset_config.num_points,
                                random_scale=aug_config.random_scale)

    test_augmentor = Augmentor(center=aug_config.center, shuffle=aug_config.shuffle, normalize=aug_config.normalize, num_points=dataset_config.num_points,
                               random_scale=False)

    path = dataset_config.path

    val_dataset = build_dataset_from_path(
        f'{path}_val.pt', truncate_canidates=dataset_config.truncate_canidates, merge_canidates=True, augmentor=test_augmentor, use_info=True)
    test_dataset = build_dataset_from_path(
        f'{path}_test.pt', truncate_canidates=dataset_config.truncate_canidates, merge_canidates=True, augmentor=test_augmentor, use_info=True)

    # for quicker load
    if not test:
        train_dataset = build_dataset_from_path(
            f'{path}_train.pt', truncate_canidates=dataset_config.truncate_canidates, merge_canidates=True, augmentor=train_augmentor)
    else:
        train_dataset = val_dataset

    print(
        f'# train: {len(train_dataset)}, # val: {len(val_dataset)}, # test: {len(test_dataset)}')
    return train_dataset, val_dataset, test_dataset


def build_dataset_from_path(path, truncate_canidates, merge_canidates, augmentor=None, use_info=False):

    x, y, info = torch.load(path)

    stats = {}
    # number of total neurites we attempt to merge
    stats['total_neurites'] = len(y)
    # number of total times we should merge before truncation of canidates
    stats['merge_opportunities'] = 0
    # the reducation in total possible success rate due to the inital truncation
    stats['truncate_succ_loss'] = 0
    stats['truncated_total_examples'] = 0
    stats['truncated_positive_examples'] = 0
    stats['truncated_negative_examples'] = 0
    stats['multi_merge'] = 0
    stats['truncated_multi_merge'] = 0
    for i in range(len(x)):
        mo = torch.sum(y[i][:, 0]).item()
        stats['merge_opportunities'] += mo
        stats['multi_merge'] += int(mo > 1)
        if truncate_canidates != 0:
            if len(y[i][truncate_canidates:]) > 0:
                stats['truncate_succ_loss'] += torch.sum(
                    y[i][truncate_canidates:][:, 0]).item()
            x[i] = x[i][:truncate_canidates]
            y[i] = y[i][:truncate_canidates]
            info[i] = info[i][:truncate_canidates]

        pe = (y[i][:, 0] == 1).count_nonzero().item()
        ne = (y[i][:, 0] == 0).count_nonzero().item()
        stats['truncated_total_examples'] += y[i].shape[0]
        stats['truncated_positive_examples'] += pe
        stats['truncated_negative_examples'] += ne
        stats['truncated_multi_merge'] += int(pe > 1)

    stats['truncate_succ_loss'] /= stats['merge_opportunities']
    stats['truncated_positive_examples'] /= stats['truncated_total_examples']
    stats['truncated_negative_examples'] /= stats['truncated_total_examples']

    if merge_canidates:
        x, y = torch.cat(x), torch.cat(y)
        info = np.array(sum(info, []))

    if not use_info:
        info = None

    ds = DatasetWithInfo(x, y, info=info, shuffle=False,
                         augmentor=augmentor, stats=stats)

    print(path, stats)

    return ds


def build_full_model_from_config(model_config: ModelConfig, dataset_config: DatasetConfig, epochs):
    # loss
    if model_config.loss == 'nll':
        loss = F.nll_loss
    elif model_config.loss == 'bce':
        loss = F.cross_entropy

    # optimizer
    if model_config.model == 'pointnet':
        model = PointNet(num_points=dataset_config.num_points,
                         classes=model_config.num_classes)
    elif model_config.model == 'curvenet':
        model = CurveNet(k=model_config.k)
    elif model_config.model == 'transnet':
        model = PointTransformerCls(
            dim=3, num_classes=model_config.num_classes)

    # optimizer
    if model_config.optimizer == 'AdamW':
        optimizer = torch.optim.AdamW(
            model.parameters(), lr=model_config.learning_rate, betas=(0.9, 0.999), weight_decay=0.05)
    # scheduler
    if model_config.scheduler == 'cos':
        scheduler = CosineAnnealingLR(
            optimizer, epochs, eta_min=1e-4)

    return model, loss, optimizer, scheduler


def get_config(name):
    for c in CONFIGS:
        if c.name == name:
            return c
    raise ValueError(f'config {name} not found.')


CONFIGS = [
    ExperimentConfig('default'),
    # cs1
    ExperimentConfig('CURVENET_ns1_cs1_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=1', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns2_cs1_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=2_cs=1', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns3_cs1_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=1', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns4_cs1_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=4_cs=1', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns5_cs1_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=1', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns6_cs1_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=6_cs=1', truncate_canidates=4)),

    # cs2
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns2_cs2_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=2_cs=2', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns4_cs2_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=4_cs=2', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns6_cs2_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=6_cs=2', truncate_canidates=4)),
    # cs3
    ExperimentConfig('CURVENET_ns4_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=4_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns5_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns6_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=6_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns7_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=7_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns8_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=8_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns9_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=9_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns10_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=10_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns11_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=11_cs=3', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns12_cs3_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=12_cs=3', truncate_canidates=4)),

    # cs4
    ExperimentConfig('CURVENET_ns1_cs4_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=4', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns2_cs4_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=2_cs=4', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns3_cs4_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=4', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns4_cs4_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=4_cs=4', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns5_cs4_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=4', truncate_canidates=4)),
    ExperimentConfig('CURVENET_ns6_cs4_t4_aligned', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=6_cs=4', truncate_canidates=4)),

    # cs2 ns3 num points
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_1024', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=1024), augmentor=AugmentorConfig(num_points=1024)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_512', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=512), augmentor=AugmentorConfig(num_points=512)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_256', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=256), augmentor=AugmentorConfig(num_points=256)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_128', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=128), augmentor=AugmentorConfig(num_points=128)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_64', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=64), augmentor=AugmentorConfig(num_points=64)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_32', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=32), augmentor=AugmentorConfig(num_points=32)),
    ExperimentConfig('CURVENET_ns3_cs2_t4_aligned_16', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=3_cs=2', truncate_canidates=4, num_points=16), augmentor=AugmentorConfig(num_points=16)),

    # cs2 ns1 num points
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned_1024', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4, num_points=1024), augmentor=AugmentorConfig(num_points=1024)),
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned_512', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4, num_points=512), augmentor=AugmentorConfig(num_points=512)),
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned_256', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4, num_points=256), augmentor=AugmentorConfig(num_points=256)),
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned_128', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4, num_points=128), augmentor=AugmentorConfig(num_points=128)),
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned_64', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4, num_points=64), augmentor=AugmentorConfig(num_points=64)),
    ExperimentConfig('CURVENET_ns1_cs2_t4_aligned_32', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=1_cs=2', truncate_canidates=4, num_points=32), augmentor=AugmentorConfig(num_points=32)),


    # cs2 ns5 num points
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned_1024', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4, num_points=1024), augmentor=AugmentorConfig(num_points=1024)),
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned_512', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4, num_points=512), augmentor=AugmentorConfig(num_points=512)),
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned_256', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4, num_points=256), augmentor=AugmentorConfig(num_points=256)),
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned_128', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4, num_points=128), augmentor=AugmentorConfig(num_points=128)),
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned_64', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4, num_points=64), augmentor=AugmentorConfig(num_points=64)),
    ExperimentConfig('CURVENET_ns5_cs2_t4_aligned_32', model=ModelConfig(model='curvenet'), dataset=DatasetConfig(
        path='/mnt/home/jberman/ceph/pf/dataset/DATASET_aligned_ns=5_cs=2', truncate_canidates=4, num_points=32), augmentor=AugmentorConfig(num_points=32)),

]
