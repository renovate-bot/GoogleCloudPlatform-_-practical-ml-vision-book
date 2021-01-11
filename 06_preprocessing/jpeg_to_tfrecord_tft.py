#!/usr/bin/env python3

# Copyright 2020 Google Inc. Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License. You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0 Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

r"""

Apache Beam pipeline to create TFRecord files from JPEG files stored on GCS.
This pipeline will split the data 80:10:10,
convert the images to lie in [-1, 1] range and resize them.

Modify the constants and TF Record format as needed.

Example usage:
python3 -m jpeg_to_tfrecord_tft \
       --all_data gs://cloud-ml-data/img/flower_photos/all_data.csv \
       --labels_file gs://cloud-ml-data/img/flower_photos/dict.txt \
       --project_id $PROJECT \
       --output_dir gs://${BUCKET}/data/flower_tfrecords \
       --resize 224,224

The format of the CSV files is:
    URL-of-image,label
And the format of the labels_file is simply a list of strings one-per-line.
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import apache_beam as beam
import tensorflow as tf
import numpy as np
import tensorflow_transform as tft
import tensorflow_transform.beam as tft_beam

def _string_feature(value):
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value.encode('utf-8')]))

def _int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=value))

def _float_feature(value):
    return tf.train.Feature(float_list=tf.train.FloatList(value=value))

def tft_preprocess(img, label):
    print(filename)
    IMG_CHANNELS = 3
    img = tf.image.decode_jpeg(img, channels=IMG_CHANNELS)
    img = tf.image.convert_image_dtype(img, tf.float32)
    img = tf.image.resize_with_pad(img, IMG_HEIGHT, IMG_WIDTH)
    img = tf.reshape(img, [-1]) # flatten to 1D array
    return img, label

def read_image(filename):
    return tf.io.read_file(filename)

def create_tfrecord(img, label, label_int):
    return tf.train.Example(features=tf.train.Features(feature={
        'image': _float_feature(img),
        'label': _string_feature(label),
        'label_int': _int64_feature([label_int])
    })).SerializeToString()

def assign_record_to_split(rec):
    rnd = np.random.rand()
    if rnd < 0.8:
        return ('train', rec)
    if rnd < 0.9:
        return ('valid', rec)
    return ('test', rec)

def yield_records_for_split(x, desired_split):
    split, rec = x
    # print(split, desired_split, split == desired_split)
    if split == desired_split:
        yield rec

def write_records(OUTPUT_DIR, splits, split):
    # same 80:10:10 split
    # The flowers dataset takes about 1GB, so 20 files means 50MB each
    nshards = 16 if (split == 'train') else 2
    _ = (splits
         | 'only_{}'.format(split) >> beam.FlatMap(
             lambda x: yield_records_for_split(x, split))
         | 'write_{}'.format(split) >> beam.io.tfrecordio.WriteToTFRecord(
             os.path.join(OUTPUT_DIR, split),
             file_name_suffix='.gz', num_shards=nshards)
        )
        
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--all_data',
        # pylint: disable=line-too-long
        help=
        'Path to input.  Each line of input has two fields  image-file-name and label separated by a comma',
        required=True)
    parser.add_argument(
        '--labels_file',
        help='Path to file containing list of labels, one per line',
        required=True)
    parser.add_argument(
        '--project_id',
        help='ID (not name) of your project. Ignored by DirectRunner',
        required=True)
    parser.add_argument(
        '--runner',
        help='If omitted, uses DataFlowRunner if output_dir starts with gs://',
        default=None)
    parser.add_argument(
        '--region',
        help='Cloud Region to run in. Ignored for DirectRunner',
        default='us-central1')
    parser.add_argument(
        '--resize',
        help='Specify the img_height,img_width that you want images resized.',
        default='224,224')
    parser.add_argument(
        '--output_dir', help='Top-level directory for TF Records', required=True)

    args = parser.parse_args()
    arguments = args.__dict__

    JOBNAME = (
            'preprocess-images-' + datetime.datetime.now().strftime('%y%m%d-%H%M%S'))

    PROJECT = arguments['project_id']
    OUTPUT_DIR = arguments['output_dir']

    # set RUNNER using command-line arg or based on output_dir path
    on_cloud = OUTPUT_DIR.startswith('gs://')
    if arguments['runner']:
        RUNNER = arguments['runner']
        on_cloud = (RUNNER == 'DataflowRunner')
    else:
        RUNNER = 'DataflowRunner' if on_cloud else 'DirectRunner'

    # clean-up output directory since Beam will name files 0000-of-0004 etc.
    # and this could cause confusion if earlier run has 0000-of-0005, for eg
    if on_cloud:
        try:
            subprocess.check_call('gsutil -m rm -r {}'.format(OUTPUT_DIR).split())
        except subprocess.CalledProcessError:
            pass
    else:
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
        os.makedirs(OUTPUT_DIR)
   
    # Use eager execution in DirectRunner, but @tf.function in DataflowRunner
    # See https://www.tensorflow.org/guide/function
    print(tf.__version__)
    #tf.config.run_functions_eagerly(not on_cloud)

    # read list of labels
    with tf.io.gfile.GFile(arguments['labels_file'], 'r') as f:
        LABELS = [line.rstrip() for line in f]
    print('Read in {} labels, from {} to {}'.format(
        len(LABELS), LABELS[0], LABELS[-1]))
    if len(LABELS) < 2:
        print('Require at least two labels')
        sys.exit(-1)

    # resize the input images
    ht, wd = arguments['resize'].split(',')
    IMG_HEIGHT = int(ht)
    IMG_WIDTH = int(wd)
    print("Will resize input images to {}x{}".format(IMG_HEIGHT, IMG_WIDTH))
        
    # make it repeatable
    np.random.seed(10)

    # set up Beam pipeline to convert images to TF Records
    options = {
        'staging_location': os.path.join(OUTPUT_DIR, 'tmp', 'staging'),
        'temp_location': os.path.join(OUTPUT_DIR, 'tmp'),
        'job_name': JOBNAME,
        'project': PROJECT,
        'max_num_workers': 20, # autoscale up to 20
        'region': arguments['region'],
        'teardown_policy': 'TEARDOWN_ALWAYS',
        'save_main_session': True
    }
    opts = beam.pipeline.PipelineOptions(flags=[], **options)

    with beam.Pipeline(RUNNER, options=opts) as p:
      with tft_beam.Context(temp_dir=tempfile.mkdtemp()):
        img_labels = (p
                  | 'read_csv' >> beam.io.ReadFromText(arguments['all_data'])
                  | 'parse_csv' >> beam.Map(lambda line: line.split(','))
                  | 'read_img' >> beam.Map(lambda x: (read_image(x[0]), x[1])))
        
        # tf.transform preprocessing
        # note that our preprocessing is simply to resize the images
        # so there is no need to be careful to run analysis only on training data       
        raw_dataset = (img_labels, 
                       tft.tf_metadata.dataset_metadata.DatasetMetadata()
                      )
        transformed_img_labels, transform_fn = (
            raw_dataset | 'tft_img' >> tft_beam.AnalyzeAndTransformDataset(tft_preprocess)
        )
       
        # write the cropped images
        splits = (transformed_img_labels
                  | 'create_tfr' >> beam.Map(lambda x: create_tfrecord(
                    x[0], x[1], LABELS.index(x[1])))
                  | 'assign_ds' >> beam.Map(assign_record_to_split)
                  )

        for split in ['train', 'valid', 'test']:
            write_records(OUTPUT_DIR, splits, split)
       
        # make sure to write out a SavedModel with the tf transforms that were carried out
        _ = (
            transform_fn | 'write_tft' >> tft_beam.WriteTransformFn(
                os.path.join(OUTPUT_DIR, 'tft'))
        )
    
        if on_cloud:
            print("Submitting {} job: {}".format(RUNNER, JOBNAME))
            print("Monitor at https://console.cloud.google.com/dataflow/jobs")
        else:
            print("Running on DirectRunner. Please hold on ...")