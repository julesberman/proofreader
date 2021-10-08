from torch.utils.data import DataLoader
from proofreader.model.config import *
from proofreader.data.cremi import prepare_cremi_vols
import click

from proofreader.data.augment import Augmentor
from typing import List
import numpy as np
import torch
import random
import cc3d
from proofreader.utils.all import list_remove
from proofreader.utils.vis import *
from proofreader.utils.data import *
from proofreader.data.augment import Augmentor

from skimage.segmentation import find_boundaries
from scipy import ndimage
from proofreader.utils.torch import *


from tqdm import tqdm


# def balance_binary_batch(batch, labels, shuffle=False):
#     """
#     Takes a batch of examples and labels of [0,1] and copies randomly samples
#     copies of which ever class is under represented until there is equal
#     representation of each class .
#     """

#     half = len(labels) // 2
#     num_1 = torch.count_nonzero(labels).squeeze()
#     num_0 = len(labels) - num_1
#     if num_0 == 0 or num_1 == 0:
#         return batch, labels
#     elif num_1 < half:
#         rebalance_label = 1
#         num = num_1
#     elif num_0 < half:
#         rebalance_label = 0
#         num = num_0
#     else:
#         return batch, labels

#     add_amt = int(half - num)
#     trues_indices = (labels == 1).nonzero().view(-1)
#     new_trues_indices = random_sample_arr(
#         trues_indices, count=add_amt, replace=True)

#     new_labels = (torch.zeros((add_amt)) + rebalance_label)

#     expanded_batch = torch.cat((batch, batch[new_trues_indices]))
#     expanded_lables = torch.cat((labels, new_labels))

#     if shuffle:
#         expanded_batch, expanded_lables = equivariant_shuffle(
#             expanded_batch, expanded_lables)

#     return expanded_batch, expanded_lables


class NeuriteDataset(torch.utils.data.Dataset):
    def __init__(self,
                 vols: List,
                 num_slices: int,
                 radius: int,
                 context_slices: int,
                 num_points: int = None,
                 open_vol: bool = True,
                 retry: bool = True,
                 epoch_multplier: int = 1,
                 shuffle: bool = False,
                 verbose: bool = False,
                 return_vol: bool = False,
                 swapaxes: bool = True,
                 torch: bool = True,
                 Augmentor: Augmentor = Augmentor(),
                 ):
        """
        Parameters:
            vols (List): list of vols to use.
            num_slices (int or 2-List[a,b]): the number of slices to drop, or range of slices to drop.
            radius (int): radius (voxels) on bottom cross section in which to select second neurite.
            context_slices (int): max number of slice on top and bottom neurites.
            num_points (int): ensures number of points in pointcloud is exactly this.
            open_vol (bool): whether to get the exterior of the vol as open tube-like manifolds (True) or as closed balloon-like manifolds (False)
                             amounts to removing the interior along the z-axis (True) or for entire vol at once (False).
            retry (bool): whether to retry until success if cannot generate example.
            epoch_multplier (int): factor which scales the effective length of the dataset. Each neurite will be sampled this many times.
            shuffle (bool): whether to shuffle the classes.
            verbose (bool): print warning messages.
            return_vol (bool): whether to also return the volumetirc representation.
            swapaxes (bool): whether to swap axes (0,1). True -> CxN, False -> NxC.
            torch (bool): whether to convert to torch tensors.
            Augmentor (PCAugmentor or None): object to peform point cloud data augmentation.
        """

        super().__init__()
        print('init dataset')
        if isinstance(num_slices, int):
            num_slices = [num_slices, num_slices]
        else:
            assert len(
                num_slices) == 2, 'if num_slices is list must be len == 2 in indicating range.'

        self.vols = vols
        self.classes = []  # all possible neurite classes
        self.class_i_to_vol_i = []  # maps a neurite class index to vol index
        self.num_slices = num_slices
        self.radius = radius
        self.context_slices = context_slices
        self.num_points = num_points
        self.open_vol = open_vol
        self.retry = retry
        self.epoch_multplier = epoch_multplier
        self.shuffle = shuffle
        self.verbose = verbose
        self.return_vol = return_vol
        self.swapaxes = swapaxes
        self.torch = torch
        self.Augmentor = Augmentor

        # get classes to use
        length = 0
        for vol_i, vol in enumerate(vols):
            # TODO figure out zspan condition other than max num_slices
            classes_z = get_classes_which_zspan_at_least(vol, num_slices[1]+2)
            classes_vol = get_classes_with_at_least_volume(vol, 400)
            cls_union = list(set(classes_z) & set(classes_vol))
            length += len(cls_union)
            self.classes.extend(cls_union)
            self.class_i_to_vol_i.extend([vol_i] * len(cls_union))

        # convert to numpy
        self.classes = np.array(self.classes)
        self.class_i_to_vol_i = np.array(self.class_i_to_vol_i)

        # shuffle classes and vol_i map together
        if self.shuffle:
            shuffler = np.random.permutation(len(self.classes))
            self.classes = self.classes[shuffler]
            self.class_i_to_vol_i = self.class_i_to_vol_i[shuffler]

        # double for positive and negative examples
        self.true_length = length*2
        self.effective_length = self.true_length * self.epoch_multplier

    def get_volumetric_example(self, vol, c, label, num_slices, radius, context_slices):
        """
        Gets a postive or negative example from vol using some class seed. Returns the volmertic representation of example.
            c (int): label of a neurite in vol to use as source
            example_type (bool): True for positive example, False for negative example
            radius (int): radius (voxels) on bottom cross section in which to select second neurite
            num_slices (int): number of slices to drop
            context_slices (int): max number of slice on top and bottom neurites
        """
        margin = 1  # number of slices that must be left on top after droping slices
        top_c = c
        (sz, sy, sx) = vol.shape

        # Find min and max z slice on which c occurs #
        for i in range(sz):
            if c in vol[i]:
                zmin = i
                break
        for i in reversed(range(sz)):
            if c in vol[i]:
                zmax = i
                break
        assert zmax - zmin >= num_slices + \
            2, f'zspan of neurite must be at least 2 slices bigger than num_slices to drop, zspan:{zmax - zmin}, num_slices:{num_slices}'

        # the drop can start at the end of the top nerutie for negative examples
        # but should start earlier such that there is some bottom fragment for postive examples
        z_max_range = zmax-margin-num_slices+1 if label == 1 else zmax+1
        # margin not needed on bottom
        drop_start = random.randint(zmin+margin, z_max_range)
        # take min to ensure there is some bottom vol
        drop_end = min(drop_start+num_slices, vol.shape[0]-1)
        top_z_len = min(context_slices, drop_start-zmin)
        bot_z_len = min(context_slices, sz-drop_end)

        if self.verbose:
            print(
                f'num_slices: {num_slices}, drop: [{drop_start}, {drop_end}]')

        # Alloc final vol, we dont know how large it will be in y and x but we know max z #
        mz = num_slices + top_z_len + bot_z_len
        final_vol = np.zeros((mz, sy, sx), dtype='uint')

        # Build top section #
        top_vol_section = final_vol[0:top_z_len]
        top_vol_section[vol[drop_start-top_z_len:drop_start] == top_c] = top_c

        # Do connected component relabeling to ensure only one fragment on top #
        top_vol_section_relabeled = cc3d.connected_components(top_vol_section)
        # must select from top border slice
        top_classes_relabeled = list(np.unique(top_vol_section_relabeled[-1]))
        top_classes_relabeled = list_remove(top_classes_relabeled, 0)
        # select new top class from relabeled top section
        relabeled_top_c = random.choice(top_classes_relabeled)
        top_vol_section_relabeled[top_vol_section_relabeled !=
                                  relabeled_top_c] = 0

        # Get midpoint of neurite on 2D top cross section, #
        top_border = top_vol_section_relabeled[-1]
        # use the relabeled top section
        (com_x, com_y) = ndimage.measurements.center_of_mass(top_border)
        (com_x, com_y) = round(com_x), round(com_y)

        # Find all neurites with distnce D from that point on bottom cross section #
        bot_border = vol[drop_end].copy()  # need copy because we zero
        mask = circular_mask(
            bot_border.shape[0], bot_border.shape[1], center=(com_y, com_x), radius=radius)
        bot_border[~mask] = 0
        mismatch_classes = list(np.unique(bot_border))

        # For positive examples, simply set bottom class to top class #
        if label == 1:
            bot_c = top_c
        else:
            # Other wise select bottom class by picking 1 neurite from set of labels in radius #
            assert mismatch_classes[0] == 0, 'first class should be 0, otherwise something went wrong'
            # remove 0 and top class lables
            mismatch_classes = list_remove(mismatch_classes, [0, top_c])
            if len(mismatch_classes) == 0:
                if self.verbose:
                    print(
                        f'(find negative) for {label} example, class {c}, cut {drop_start, drop_end} could not find bottom label within radius, returning none')
                return None
            # maybe could select here based on on cross-sectional volume
            # select bottom neurite class
            bot_c = random.choice(mismatch_classes)

        # Build bot section #
        bot_vol_section = final_vol[num_slices+top_z_len:]
        bot_vol_section[vol[drop_end:drop_end+bot_z_len] == bot_c] = bot_c

        # Do connected component relabeling to ensure only one fragment on bottom #
        # The mask and radius are needed for both positive and negative examples #
        # So that after connected components we can pick a fragment near the top neurite #
        bot_vol_section_relabeled = cc3d.connected_components(bot_vol_section)

        bot_border_relabled = bot_vol_section_relabeled[0]
        relabeled_fragments_in_radius = list(
            np.unique(bot_border_relabled[mask]))
        relabeled_fragments_in_radius = list_remove(
            relabeled_fragments_in_radius, 0)
        if len(relabeled_fragments_in_radius) == 0:
            if self.verbose:
                print(
                    f'(relabel bot) for {label} example, class {c}, cut {drop_start, drop_end}, could not find bottom label within radius, returning none')
            return None
        # take fragment which is in radius
        relabeled_bot_c = random.choice(relabeled_fragments_in_radius)
        bot_vol_section_relabeled[bot_vol_section_relabeled !=
                                  relabeled_bot_c] = 0

        # Build final volume of top and bottom sections #
        final_vol[0: top_z_len] = top_vol_section_relabeled
        final_vol[num_slices+top_z_len:] = bot_vol_section_relabeled

        return final_vol

    def get_vol_class_label_from_index(self, index):

        index = index % self.true_length
        label = np.int64(index % 2 == 0)  # label based on even or odd
        index = index // 2
        c = self.classes[index]
        vol_i = self.class_i_to_vol_i[index]
        vol = self.vols[vol_i]

        if self.verbose:
            print(f'{label}, vol: {vol_i}, c: {c}')

        return vol, c, label

    def convert_to_point_cloud(self, vol):

        pc = convert_grid_to_pointcloud(vol)

        if self.num_points is not None:
            num_points = pc.shape[0]

            if num_points < self.num_points:
                if self.verbose:
                    print(
                        f'not enough points, need {self.num_points}, have {num_points}, replace sampling to fix')

                pc = random_sample_arr(
                    pc, count=self.num_points, replace=True)

            else:
                pc = random_sample_arr(pc, count=self.num_points)

        return pc

    def remove_vol_interiors(self, vol):

        def rm_interior(v):
            return v * find_boundaries(
                v, mode='inner')

        if self.open_vol:
            for i in range(vol.shape[0]):
                vol[i] = rm_interior(vol[i])
        else:
            vol = rm_interior(vol)

        return vol

    def get_example(self, index):

        vol, c, label = self.get_vol_class_label_from_index(index)

        # choose num_slices
        num_slices = random.randint(self.num_slices[0], self.num_slices[1])
        vol_example = self.get_volumetric_example(
            vol, c, label, num_slices, self.radius, self.context_slices)

        # if we cant build example, try again with random index
        if self.retry and vol_example is None:
            rand_i = random.randint(0, self.effective_length)
            if self.verbose:
                print(f'redo on i {rand_i}')
            return self.get_example(rand_i)

        # final crop and relabel
        vol_example = crop_where(vol_example, vol_example != 0)
        vol_example = cc3d.connected_components(vol_example)

        # sanity check
        all_classes = np.unique(vol_example)
        assert len(
            all_classes) == 3, f'final sample should have 3 classes, [0, n1, n2] not {all_classes}'

        # remove interiors
        vol_example = self.remove_vol_interiors(vol_example)

        # convert to point cloud
        pc_example = self.convert_to_point_cloud(vol_example)
        if self.Augmentor is not None:
            pc_example = self.Augmentor.transfrom(pc_example)

        # swap axes if needed
        if self.swapaxes:
            pc_example = np.swapaxes(pc_example, 0, 1)

        if self.verbose:
            print(
                f'pc shape: {pc_example.shape}, vol shape: {vol_example.shape}')

        if self.torch:
            pc_example = torch.from_numpy(pc_example).type(torch.float32)
            label = torch.tensor(label).type(torch.LongTensor)

        if self.return_vol:
            return (pc_example, label, vol_example)

        return (pc_example, label)

    def __getitem__(self, index):

        return self.get_example(index)

    def __len__(self):
        return self.effective_length


class SliceDataset(torch.utils.data.IterableDataset):
    def __init__(self,
                 vols: List,
                 num_slices: int,
                 radius: int,
                 context_slices: int,
                 num_points: int = None,
                 add_batch_id: bool = False,
                 truncate_candidates: bool = False,
                 return_candidate_batch: bool = False,
                 randomize: bool = False,
                 Augmentor: Augmentor = Augmentor(),
                 verbose: bool = False,
                 ):

        # clean vols
        self.vols = []
        for vol in vols:
            vol = zero_classes_with_min_volume(vol, 500)
            self.vols.append(vol)

        self.num_slices = num_slices
        self.radius = radius
        self.context_slices = context_slices
        self.num_points = num_points
        self.Augmentor = Augmentor
        self.verbose = verbose
        self.add_batch_id = add_batch_id
        self.truncate_candidates = truncate_candidates
        self.return_candidate_batch = return_candidate_batch
        self.randomize = randomize

        self.test_iteration_batch = None
        self.test_iteration_i = 0
        self.test_iteration_len = 0

        self.top_neurites = np.zeros((0))
        self.cur_neurite_i = 0
        self.vol_relabeled = None
        self.label_map = None

        self.cur_drop_start = context_slices-1
        self.cur_vol_i = 0
        self.worker_id = 0
        # stats
        self.no_true = 0
        self.multi_true = 0
        self.num_true = 0
        self.total_examples = 0

    def load_next_candidate_batch(self):

        if self.cur_neurite_i >= self.top_neurites.shape[0]:
            if self.verbose:
                print('getting all top neurites for drop')

            # only increment if its not the init or for randomize
            if self.top_neurites.shape[0] > 0 or self.randomize:
                self.increment_vol_and_drop()

            vol = self.get_cur_vol()
            (drop_start, drop_end) = self.get_cur_drop()

            cs = self.context_slices

            # build a new vol with slices dropped in the middle
            # and do connected_components do relablel/detach neurites on
            # either side of the volume
            vol_relabeled = np.zeros_like(vol)
            vol_relabeled[drop_start -
                          cs:drop_start] = vol[drop_start-cs:drop_start]
            vol_relabeled[drop_end:drop_end+cs] = vol[drop_end:drop_end+cs]

            # remeber where the background is then reset in after cc
            zero_indices = vol_relabeled == 0
            vol_relabeled = cc3d.connected_components(vol_relabeled)
            vol_relabeled[zero_indices] = 0

            # create a map from the new lables to the original labels
            # this allows us to figure out the ground truth for accuracy
            label_map = correspond_labels(vol_relabeled, vol, bg_label=0)

            # take the neurites on the top border of the missing slices
            # and attempt to reattach
            top_neurites = np.unique(vol_relabeled[drop_start-1])
            top_neurites = np.delete(top_neurites, 0)
            np.random.shuffle(top_neurites)
            self.top_neurites = top_neurites
            self.cur_neurite_i = 0
            self.vol_relabeled = vol_relabeled
            self.label_map = label_map

        if self.verbose:
            print('getting top neurite batch')

        drop = self.get_cur_drop()
        c = self.top_neurites[self.cur_neurite_i]

        examples, labels = self.get_examples_from_top_class(
            self.vol_relabeled, c, drop, self.label_map)

        # get stats and cut off
        num_true = labels.count_nonzero()
        if num_true == 0:
            self.no_true += 1
        if num_true > 1:
            self.multi_true += 1

        # crop examples
        if self.truncate_candidates and num_true >= 1:
            true_i = list(labels).index(1)
            labels = labels[:true_i+1]
            examples = examples[:true_i+1]

        # more stats
        self.num_true += labels.count_nonzero().item()
        self.total_examples += examples.shape[0]
        # examples, labels = equivariant_shuffle(
        #     examples, labels)

        if self.add_batch_id:
            candidate_batch_num = int(
                f'{self.cur_vol_i}{drop[0]}{self.cur_neurite_i}')
            ids = torch.zeros_like(labels) + candidate_batch_num
            labels = torch.stack((labels, ids), dim=1)

        self.cur_neurite_i += 1
        self.test_iteration_batch = (examples, labels)
        self.test_iteration_len = examples.shape[0]
        self.test_iteration_i = 0

    def get_examples_from_top_class(self, vol, c, drop, label_map):

        top_c = c
        (sz, sy, sx) = vol.shape

        # Find min and max z slice on which c occurs #
        for i in range(sz):
            if c in vol[i]:
                zmin = i
                break
        # assert zmax - zmin >= num_slices + \
        #     2, f'zspan of neurite must be at least 2 slices bigger than num_slices to drop, zspan:{zmax - zmin}, num_slices:{num_slices}'

        drop_start, drop_end = drop
        num_slices = drop_end - drop_start
        top_z_len = min(self.context_slices, drop_start-zmin)
        bot_z_len = min(self.context_slices, sz-drop_end)

        # Alloc final vol, we dont know how large it will be in y and x but we know max z #
        mz = num_slices + top_z_len + bot_z_len
        final_vol = np.zeros((mz, sy, sx), dtype='uint')

        # Build top section #
        top_vol_section = final_vol[0:top_z_len]
        top_vol_section[vol[drop_start-top_z_len:drop_start] == top_c] = top_c

        # Get midpoint of neurite on 2D top cross section, #
        top_border = top_vol_section[-1]
        # use the relabeled top section
        (com_x, com_y) = ndimage.measurements.center_of_mass(top_border)
        (com_x, com_y) = round(com_x), round(com_y)

        # Find all neurites with distnce D from that point on bottom cross section #
        bot_border = vol[drop_end].copy()  # need copy because we zero
        mask = circular_mask(
            bot_border.shape[0], bot_border.shape[1], center=(com_y, com_x), radius=self.radius)
        bot_border[~mask] = 0
        mismatch_classes = list(np.unique(bot_border))

        # # Other wise select bottom class by picking 1 neurite from set of labels in radius #
        # assert mismatch_classes[0] == 0, 'first class should be 0, otherwise something went wrong'
        # # remove 0
        # mismatch_classes = list_remove(mismatch_classes, 0)

        # DISTANCE SORT #
        # get classes in order of distance from top neurite for efficieny we just look at the top_border and bot_border stack
        d_vol = np.stack([top_border, bot_border])
        d_vol = crop_where(d_vol, d_vol != 0)
        mismatch_classes = get_classes_sorted_by_distance(
            d_vol, top_c, method='mean')

        final_vol[0: top_z_len] = top_vol_section
        final_examples = torch.zeros(
            (len(mismatch_classes), 3, self.num_points))
        final_lables = []
        for i, bot_c in enumerate(mismatch_classes):

            cur_vol = final_vol.copy()
            # Build bot section #
            bot_vol_section = cur_vol[num_slices+top_z_len:]
            bot_vol_section[vol[drop_end:drop_end+bot_z_len] == bot_c] = bot_c

            # Build final volume of bottom sections #
            cur_vol[num_slices+top_z_len:] = bot_vol_section

            pc = self.convert_volumetric_to_final(cur_vol)
            final_examples[i] = pc
            label = int(label_map[top_c] == label_map[bot_c])
            final_lables.append(label)

        return final_examples, torch.tensor(final_lables)

    def remove_vol_interiors(self, vol):

        def rm_interior(v):
            return v * find_boundaries(
                v, mode='inner')

        for i in range(vol.shape[0]):
            vol[i] = rm_interior(vol[i])

        return vol

    def convert_to_point_cloud(self, vol):

        pc = convert_grid_to_pointcloud(vol)
        if self.num_points is not None:
            num_points = pc.shape[0]

            if num_points < self.num_points:
                pc = random_sample_arr(
                    pc, count=self.num_points, replace=True)

            else:
                pc = random_sample_arr(pc, count=self.num_points)

        return pc

    def convert_volumetric_to_final(self, vol_example):

        # final crop and relabel
        vol_example = crop_where(vol_example, vol_example != 0)
        vol_example = cc3d.connected_components(vol_example)

        # sanity check
        all_classes = np.unique(vol_example)
        assert len(
            all_classes) == 3, f'final sample should have 3 classes, [0, n1, n2] not {all_classes}'

        # grid_volume(color.label2rgb(vol_example, bg_label=0))

        # remove interiors
        vol_example = self.remove_vol_interiors(vol_example)

        # convert to point cloud
        pc_example = self.convert_to_point_cloud(vol_example)
        if self.Augmentor is not None:
            pc_example = self.Augmentor.transfrom(pc_example)
        pc_example = np.swapaxes(pc_example, 0, 1)

        pc_example = torch.from_numpy(pc_example).type(torch.float32)

        return pc_example

    def get_drop_start_range(self):
        # point at each drop end cannot exceede
        cur_vol = self.get_cur_vol()
        range_start = self.context_slices
        range_stop = cur_vol.shape[0] - self.num_slices - self.context_slices
        # for worker parallelization set drop_end_max by drop_start_worker_range
        if self.drop_start_worker_range is not None:
            range_start, range_stop = self.drop_start_worker_range

        return (range_start, range_stop)

    def increment_vol_and_drop(self):
        if self.verbose:
            print('increment drop')
        self.cur_drop_start += 1
        range_start, range_stop = self.get_drop_start_range()
        if self.randomize:
            self.cur_drop_start = random.randint(range_start, range_stop-1)
            self.cur_vol_i = random.randint(0, len(self.vols)-1)
            print(
                f'random drop start {self.cur_drop_start} vol {self.cur_vol_i}')

        # if we have reached the end of the vol, do to next vol
        if self.cur_drop_start >= range_stop:
            self.cur_vol_i += 1
            self.cur_drop_start = range_start
            if self.verbose:
                print('increment vol')
            if self.cur_vol_i >= len(self.vols):
                raise StopIteration

    def get_cur_vol(self):
        return self.vols[self.cur_vol_i]

    def get_cur_drop(self):
        cur_drop_end = self.cur_drop_start + self.num_slices
        return (self.cur_drop_start, cur_drop_end)

    def get_stats(self):
        return f'no_true: {self.no_true}, multi_true: {self.multi_true}, num_true: {self.num_true}, total: {self.total_examples})'

    def get_next(self):
        # return entire batch of canidates
        if self.return_candidate_batch:
            self.load_next_candidate_batch()
            (all_examples, all_lables) = self.test_iteration_batch
            return all_examples, all_lables

        # if we have reached the end of the current batch load a new one
        if self.test_iteration_i >= self.test_iteration_len:
            if self.verbose:
                print('finish single top neurite batch')
            self.load_next_candidate_batch()

        # get the example from the batch in object state
        (all_examples, all_lables) = self.test_iteration_batch

        if self.verbose:
            print(
                f'vol: {self.cur_vol_i}, drop: {self.get_cur_drop()}, neurite: {self.cur_neurite_i}, candidate: {self.test_iteration_i}')

        x, y = all_examples[self.test_iteration_i], all_lables[self.test_iteration_i]
        self.test_iteration_i += 1

        return x, y

    def build_generator(self, drop_start_worker_range=None):
        # for worker parallelization
        self.drop_start_worker_range = drop_start_worker_range
        if drop_start_worker_range is not None:
            worker_info = torch.utils.data.get_worker_info()
            print(
                f'worker_id: {worker_info.id}, drop_start_worker_range: {drop_start_worker_range}')
            self.worker_id = worker_info.id

        range_start, _ = self.get_drop_start_range()
        self.cur_drop_start = range_start
        # generator
        while True:
            try:
                yield self.get_next()
            except StopIteration:
                return

    def __iter__(self):
        worker_info = torch.utils.data.get_worker_info()
        if worker_info is None:  # single-process data loading, return the full iterator
            return self.build_generator()
        else:  # in a worker process
            vol = self.get_cur_vol()  # assumes vols all same shape
            start, end = self.context_slices, vol.shape[0] - \
                self.num_slices - self.context_slices - 1
            # split workload
            per_worker = int(
                math.ceil((end - start) / float(worker_info.num_workers)))
            worker_id = worker_info.id
            iter_start = start + worker_id * per_worker
            iter_end = min(iter_start + per_worker, end)
            drop_start_worker_range = (iter_start, iter_end)
            if iter_start >= iter_end:
                print(
                    f'too many workers, not using worker {worker_id}')
                return iter(())
            return self.build_generator(drop_start_worker_range=drop_start_worker_range)


@click.command()
@click.option('--output-dir', '-o',
              type=str, default='/mnt/home/jberman/ceph/pf/dataset',
              help='for output'
              )
@click.option('--num_slices', '-ns',
              type=int,
              help='num slices to drop'
              )
@click.option('--context_slices', '-cs',
              type=int,
              help='num of slices for context on each neurite'
              )
@click.option('--num_points', '-np',
              type=int, default=2048,
              )
@click.option('--radius', '-r',
              type=int, default=128,
              )
@click.option('--num_workers', '-w',
              type=int, default=-1,
              help='num workers for pytorch dataloader. -1 means automatically set.'
              )
def generate_dataset(output_dir: str, num_slices: int, context_slices: int, num_points: int, radius: int, num_workers: int):

    augmentor = Augmentor(center=True, shuffle=True,
                          normalize=(125, 1250, 1250))

    name = f'ns={num_slices}|r={radius}|cs={context_slices}|np={num_points}'

    # auto set
    if num_workers == -1:
        num_workers = get_cpu_count()
    batch_size = 256

    # file management
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    version = 0
    file_path = f'{output_dir}/{name}_dataset'
    while os.path.exists(f'{file_path}_{version}'):
        version += 1
    file_path = f'{file_path}_{version}'
    print(
        f'\nname {name}\nbatch_size {batch_size}, num_workers {num_workers}')
    print(f'file_path {file_path}...')

    print(f'loading volumes...')
    validation_slices = num_slices + (context_slices*2)+1
    print('validation_slices', validation_slices)
    train_vols, val_vols, test_vols = prepare_cremi_vols(
        './dataset/cremi', validation_slices=validation_slices)
    print(
        f'| train {train_vols[0].shape} | val {val_vols[0].shape} | test {test_vols[0].shape} |')

    for vols, name in zip([train_vols, val_vols, test_vols], ['train', 'val', 'test']):
        print(f'generating data for {name} set...')

        is_test = name == 'test'

        dataset = SliceDataset(vols, num_slices, radius, context_slices, num_points=num_points, Augmentor=augmentor,
                               add_batch_id=True, truncate_candidates=True, return_candidate_batch=is_test, verbose=False)

        if not is_test:
            iterator = DataLoader(
                dataset=dataset, batch_size=batch_size, num_workers=num_workers, drop_last=False, persistent_workers=True)
        else:
            iterator = dataset

        all_x, all_y = [], []
        for step, batch in tqdm(enumerate(iterator)):
            x, y = batch
            all_x.append(x)
            all_y.append(y)

        print(dataset.get_stats())
        if not is_test:
            print(f'concating batches...')
            all_x = torch.cat(all_x)
            all_y = torch.cat(all_y)
        print(f'x: {all_x.shape} y: {all_y.shape}')

        print(f'saving batches...')
        torch.save((all_x, all_y), f'{file_path}_{name}.pt')

        print(f'finished {name}!')


if __name__ == '__main__':
    generate_dataset()
