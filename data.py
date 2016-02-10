import glob
import re
import numpy as np
import skimage.io
import skimage.transform
from configuration import config
import cPickle as pickle
import compressed_cache
from collections import namedtuple


def read_labels(file_path):
    id2labels = {}
    train_csv = open(file_path)
    lines = train_csv.readlines()
    i = 0
    for item in lines:
        if i == 0:
            i = 1
            continue
        id, systole, diastole = item.replace('\n', '').split(',')
        id2labels[int(id)] = [float(systole), float(diastole)]
    return id2labels


def read_patients_data(paths):
    data_dict = {}
    for p in paths:
        patient_id = int(re.search(r'/(\d+)/', p).group(1))
        data_dict[patient_id] = read_patient(p)
    return data_dict


def read_patient(path):
    slices_paths = sorted(glob.glob(path + '/*.pkl'))
    slices_dict = {}
    for sp in slices_paths:
        slice = re.search(r'/(\w+\d+)*\.pkl$', sp).group(1)
        slices_dict[slice] = read_slice(sp)
    return slices_dict


@compressed_cache.memoize()
def read_slice(path):
    return pickle.load(open(path))['data']


@compressed_cache.memoize()
def read_metadata(path):
    d = pickle.load(open(path))['metadata'][0]
    metadata = {k: d[k] for k in ['PixelSpacing', 'ImageOrientationPatient', 'SliceLocation',
                                  'PatientSex', 'PatientAge']}
    metadata['PixelSpacing'] = np.float32(metadata['PixelSpacing'])
    metadata['ImageOrientationPatient'] = np.float32(metadata['ImageOrientationPatient'])
    metadata['SliceLocation'] = np.float32(metadata['SliceLocation'])
    metadata['PatientSex'] = 1 if metadata['PatientSex'] == 'F' else 0
    metadata['PatientAge'] = int(metadata['PatientAge'][1:3])
    return metadata


def sample_augmentation_parameters(transformation):
    random_params = None
    if all([transformation['rotation_range'],
            transformation['translation_range'],
            transformation['shear_range'],
            transformation['do_flip'],
            transformation['sequence_shift']]):
        shift_x = config().rng.uniform(*transformation['translation_range'])
        shift_y = config().rng.uniform(*transformation['translation_range'])
        translation = (shift_x, shift_y)
        rotation = config().rng.uniform(*transformation['rotation_range'])
        shear = config().rng.uniform(*transformation['shear_range'])
        flip = (config().rng.randint(2) > 0) if transformation['do_flip'] else False  # flip half of the time
        sequence_shift = config().rng.randint(30) if transformation['sequence_shift'] else 0
        random_params = namedtuple('Params', ['translation', 'rotation', 'shear', 'flip', 'sequence_shift'])(
            translation,
            rotation,
            shear, flip,
            sequence_shift)
    return random_params


def transform_with_metadata(data, metadata, transformation, random_augmentation_params=None):
    """
    :param data: (30, height, width) matrix from one slice of MRI
    :param transformation:
    :return:
    """

    out_shape = (30,) + transformation['patch_size']
    out_data = np.zeros(out_shape, dtype='float32')

    # if random_augmentation_params=None -> sample new params
    # if the transformation implies no augmentations then random_augmentation_params remains None
    if not random_augmentation_params:
        random_augmentation_params = sample_augmentation_parameters(transformation)

    # rotate images and fix the contrast
    data = fix_contrasts(data)

    # build scaling transformation
    scaling = max(1. * data.shape[-2] / out_shape[-2], 1. * data.shape[-1] / out_shape[-1])
    tform = build_rescale_transform(scaling, data.shape[-2:], target_shape=transformation['patch_size'])
    orient_tform = build_orientation_correction_transform(metadata)
    tform_center, tform_uncenter = build_center_uncenter_transforms(data.shape[-2:])
    total_tform = tform + tform_uncenter + orient_tform + tform_center

    # calculate area per pixel in the rescaled image
    zoom_ratio = []
    A = tform.params[:2, :2]
    zoom_ratio.append(np.linalg.norm(A[:, 0]) * np.linalg.norm(A[:, 1]))
    assert tform.params[2, 2] == 1, (tform.params[2, 2],)
    area_per_pixel = zoom_ratio[0] * np.prod(metadata["PixelSpacing"])

    # build random augmentation
    if random_augmentation_params:
        augment_tform = build_augmentation_transform(rotation=random_augmentation_params.rotation,
                                                     shear=random_augmentation_params.shear,
                                                     translation=random_augmentation_params.translation,
                                                     flip=random_augmentation_params.flip)
        total_tform = tform + tform_uncenter + augment_tform + orient_tform + tform_center

    # apply transformation per image
    for i in xrange(data.shape[0]):
        out_data[i] = fast_warp(data[i], total_tform, output_shape=transformation['patch_size'])

    # if the sequence is < 30 timesteps, copy last image
    if data.shape[0] < out_shape[0]:
        for j in xrange(data.shape[0], out_shape[0]):
            out_data[j] = out_data[-1]

    # if > 30, remove images
    if data.shape[0] > out_shape[0]:
        out_data = out_data[:30]

    # shift the sequence for a number of time steps
    if random_augmentation_params:
        out_data = np.roll(out_data, random_augmentation_params.sequence_shift, axis=0)

    return out_data, area_per_pixel


def transform(data, transformation, random_augmentation_params=None):
    """
    :param data: (30, height, width) matrix from one slice of MRI
    :param transformation:
    :return:
    """
    out_shape = (30,) + transformation['patch_size']
    out_data = np.zeros(out_shape, dtype='float32')

    # if random_augmentation_params=None -> sample new params
    # if the transformation implies no augmentations then random_augmentation_params remains None
    if not random_augmentation_params:
        random_augmentation_params = sample_augmentation_parameters(transformation)

    # if images are oriented horizontally -> rotate 90 deg
    if data.shape[-1] > data.shape[-2]:
        data = np.transpose(data, (0, 2, 1))

    # build scaling transform
    scaling = max(1. * data.shape[-2] / out_shape[-2], 1. * data.shape[-1] / out_shape[-1])
    tform = build_rescale_transform(scaling, data.shape[-2:], target_shape=transformation['patch_size'])
    total_tform = tform

    # build random augmentation
    if random_augmentation_params:
        tform_center, tform_uncenter = build_center_uncenter_transforms(data.shape[-2:])

        augment_tform = build_augmentation_transform(rotation=random_augmentation_params.rotation,
                                                     shear=random_augmentation_params.shear,
                                                     translation=random_augmentation_params.translation,
                                                     flip=random_augmentation_params.flip)
        total_tform = tform + tform_uncenter + augment_tform + tform_center

    # apply transformation per image
    for i in xrange(data.shape[0]):
        out_data[i] = fast_warp(data[i], total_tform, output_shape=transformation['patch_size'])

    # if the sequence is < 30 timesteps, copy last image
    if data.shape[0] < out_shape[0]:
        for j in xrange(data.shape[0], out_shape[0]):
            out_data[j] = out_data[-1]

    # if > 30, remove images
    if data.shape[0] > out_shape[0]:
        out_data = out_data[:30]

    return out_data


tform_identity = skimage.transform.AffineTransform()


def fast_warp(img, tf, output_shape, mode='constant', order=1):
    """
    This wrapper function is faster than skimage.transform.warp
    """
    m = tf.params  # tf._matrix is
    return skimage.transform._warps_cy._warp_fast(img, m, output_shape=output_shape, mode=mode, order=order)


def build_centering_transform(image_shape, target_shape=(50, 50)):
    rows, cols = image_shape
    trows, tcols = target_shape
    shift_x = (cols - tcols) / 2.0
    shift_y = (rows - trows) / 2.0
    return skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))


def build_rescale_transform(downscale_factor, image_shape, target_shape):
    """
    estimating the correct rescaling transform is slow, so just use the
    downscale_factor to define a transform directly. This probably isn't
    100% correct, but it shouldn't matter much in practice.
    """
    rows, cols = image_shape
    trows, tcols = target_shape
    tform_ds = skimage.transform.AffineTransform(scale=(downscale_factor, downscale_factor))

    # centering
    shift_x = cols / (2.0 * downscale_factor) - tcols / 2.0
    shift_y = rows / (2.0 * downscale_factor) - trows / 2.0
    tform_shift_ds = skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))
    return tform_shift_ds + tform_ds


def build_center_uncenter_transforms(image_shape):
    """
    These are used to ensure that zooming and rotation happens around the center of the image.
    Use these transforms to center and uncenter the image around such a transform.
    """
    center_shift = np.array(
        [image_shape[1], image_shape[0]]) / 2.0 - 0.5  # need to swap rows and cols here apparently! confusing!
    tform_uncenter = skimage.transform.SimilarityTransform(translation=-center_shift)
    tform_center = skimage.transform.SimilarityTransform(translation=center_shift)
    return tform_center, tform_uncenter


def build_augmentation_transform(rotation=0, shear=0, translation=(0, 0), flip=False, zoom=(1.0, 1.0)):
    if flip:
        shear += 180
        # shear by 180 degrees is equivalent to flip along the X-axis

    tform_augment = skimage.transform.AffineTransform(scale=(1 / zoom[0], 1 / zoom[1]), rotation=np.deg2rad(rotation),
                                                      shear=np.deg2rad(shear), translation=translation)
    return tform_augment


def fix_contrasts(data):
    """
    normalizes the contrast over slice
    :param data:  data from one slice (30, h, w)
    :return:
    """
    perc = np.percentile(data[0], q=[5, 95])
    low, high = perc[0], perc[1]
    out_data = np.clip(1. * (data - low) / (high - low), 0.0, 1.0)
    return out_data


def build_orientation_correction_transform(metadata):
    tform_list = [tform_identity]

    F = np.array(metadata["ImageOrientationPatient"]).reshape((2, 3))
    fy = F[1, :]
    fx = F[0, :]

    # unit vectors of patient coordinates
    x_e = np.array([1, 0, 0])
    y_e = np.array([0, 1, 0])

    if abs(np.dot(y_e, fy)) >= abs(np.dot(y_e, fx)):
        tform_list.append(skimage.transform.AffineTransform(rotation=np.deg2rad(90)))
        tform_list.append(skimage.transform.AffineTransform(shear=np.deg2rad(180)))
        fy, fx = fx, fy

    if np.dot(y_e, fy) < 0:
        tform_list.append(skimage.transform.AffineTransform(shear=np.deg2rad(180)))

    if np.dot(x_e, fx) < 0:
        tform_list.append(skimage.transform.AffineTransform(shear=np.deg2rad(180)))
        tform_list.append(skimage.transform.AffineTransform(rotation=np.deg2rad(180)))

    tform_total = tform_identity

    for t in tform_list[::-1]:
        tform_total += t

    return tform_total
