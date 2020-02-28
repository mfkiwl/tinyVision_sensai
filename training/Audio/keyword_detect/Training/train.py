# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
r"""Simple speech recognition to spot a limited number of keywords.

This is a self-contained example script that will train a very basic audio
recognition model in TensorFlow. It downloads the necessary training data and
runs with reasonable defaults to train within a few hours even only using a CPU.
For more information, please see
https://www.tensorflow.org/tutorials/audio_recognition.

It is intended as an introduction to using neural networks for audio
recognition, and is not a full speech recognition system. For more advanced
speech systems, I recommend looking into Kaldi. This network uses a keyword
detection style to spot discrete words from a small vocabulary, consisting of
"yes", "no", "up", "down", "left", "right", "on", "off", "stop", and "go".

To run the training process, use:

bazel run tensorflow/examples/speech_commands:train

This will write out checkpoints to /tmp/speech_commands_train/, and will
download over 1GB of open source training data, so you'll need enough free space
and a good internet connection. The default data is a collection of thousands of
one-second .wav files, each containing one spoken word. This data set is
collected from https://aiyprojects.withgoogle.com/open_speech_recording, please
consider contributing to help improve this and other models!

As training progresses, it will print out its accuracy metrics, which should
rise above 90% by the end. Once it's complete, you can run the freeze script to
get a binary GraphDef that you can easily deploy on mobile applications.

If you want to train on your own data, you'll need to create .wavs with your
recordings, all at a consistent length, and then arrange them into subfolders
organized by label. For example, here's a possible file structure:

my_wavs >
  up >
    audio_0.wav
    audio_1.wav
  down >
    audio_2.wav
    audio_3.wav
  other>
    audio_4.wav
    audio_5.wav

You'll also need to tell the script what labels to look for, using the
`--wanted_words` argument. In this case, 'up,down' might be what you want, and
the audio in the 'other' folder would be used to train an 'unknown' category.

To pull this all together, you'd run:

bazel run tensorflow/examples/speech_commands:train -- \
--data_dir=my_wavs --wanted_words=up,down

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os.path
import sys, csv

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow.compat.v1 as tf

import input_data
import models
from tensorflow.python.platform import gfile

import seaborn as sns
import pandas as pd
from sklearn.metrics import classification_report as classreport

FLAGS = None


def main(_):
    # We want to see all the logging messages for this tutorial.
    tf.logging.set_verbosity(tf.logging.INFO)

    # Start a new TensorFlow session.
    sess = tf.InteractiveSession()

    # Begin by making sure we have the training data we need. If you already have
    # training data of your own, use `--data_url= ` on the command line to avoid
    # downloading.
    model_settings = models.prepare_model_settings(
        len(input_data.prepare_words_list(FLAGS.wanted_words.split(','))),
        FLAGS.sample_rate, FLAGS.clip_duration_ms, FLAGS.window_size_ms,
        FLAGS.window_stride_ms, FLAGS.dct_coefficient_count)
    audio_processor = input_data.AudioProcessor(
        FLAGS.data_url, FLAGS.data_dir, FLAGS.silence_percentage,
        FLAGS.unknown_percentage,
        FLAGS.wanted_words.split(','), FLAGS.validation_percentage,
        FLAGS.testing_percentage, model_settings)
    desired_samples = model_settings['desired_samples']
    label_count = model_settings['label_count']
    time_shift_samples = int((FLAGS.time_shift_ms * FLAGS.sample_rate) / 1000)
    # Figure out the learning rates for each training phase. Since it's often
    # effective to have high learning rates at the start of training, followed by
    # lower levels towards the end, the number of steps and learning rates can be
    # specified as comma-separated lists to define the rate at each stage. For
    # example --how_many_training_steps=10000,3000 --learning_rate=0.001,0.0001
    # will run 13,000 training loops in total, with a rate of 0.001 for the first
    # 10,000, and 0.0001 for the final 3,000.
    training_steps_list = list(
        map(int, FLAGS.how_many_training_steps.split(',')))
    learning_rates_list = list(map(float, FLAGS.learning_rate.split(',')))
    if len(training_steps_list) != len(learning_rates_list):
        raise Exception(
            '--how_many_training_steps and --learning_rate must be equal length '
            'lists, but are %d and %d long instead' % (len(training_steps_list),
                                                       len(learning_rates_list)))

    audio_input = tf.placeholder(
        tf.float32, [None, desired_samples], name='audio_input')
    if FLAGS.freeze == True:
        logits, fingerprint_4d = models.create_model(
            audio_input,
            model_settings,
            FLAGS.model_architecture,
            is_training=False,
            norm_binw=FLAGS.norm_binw,
            downsample=FLAGS.downsample,
            lock_prefilter=FLAGS.lock_prefilter,
            add_prefilter_bias=FLAGS.prefilter_bias,
            use_down_avgfilt=FLAGS.use_down_avgfilt)
        # Save graph.pbtxt.(will be used to feeze the model)
        tf.train.write_graph(sess.graph_def, FLAGS.train_dir,
                             FLAGS.model_architecture + '.pbtxt')
        print("saved pbtxt for inference at log direcory:"+FLAGS.train_dir)
        sys.exit()
    logits, dropout_prob, fingerprint_4d = models.create_model(
        audio_input,
        model_settings,
        FLAGS.model_architecture,
        is_training=True,
        norm_binw=FLAGS.norm_binw,
        downsample=FLAGS.downsample,
        lock_prefilter=FLAGS.lock_prefilter,
        add_prefilter_bias=FLAGS.prefilter_bias,
        use_down_avgfilt=FLAGS.use_down_avgfilt)
    # Define loss and optimizer
    ground_truth_input = tf.placeholder(
        tf.int64, [None], name='groundtruth_input')

    # Optionally we can add runtime checks to spot when NaNs or other symptoms of
    # numerical errors start occurring during training.
    control_dependencies = []
    if FLAGS.check_nans:
        checks = tf.add_check_numerics_ops()
        control_dependencies = [checks]

    # Create the back propagation and training evaluation machinery in the graph.
    with tf.name_scope('cross_entropy'):
        cross_entropy_mean = tf.losses.sparse_softmax_cross_entropy(
            labels=ground_truth_input, logits=logits)
    tf.summary.scalar('cross_entropy', cross_entropy_mean)
    with tf.name_scope('train'), tf.control_dependencies(control_dependencies):
        learning_rate_input = tf.placeholder(
            tf.float32, [], name='learning_rate_input')
        if FLAGS.optimizer == 'SGD':
            train_step = tf.train.GradientDescentOptimizer(
                learning_rate_input).minimize(cross_entropy_mean)
        elif FLAGS.optimizer.upper() == 'ADAM':
            train_step = tf.train.AdamOptimizer(
                learning_rate_input).minimize(cross_entropy_mean)
        else:
            raise Exception(
                'Unknown optimizer, '+FLAGS.optimizer)
    predicted_indices = tf.argmax(logits, 1)
    correct_prediction = tf.equal(predicted_indices, ground_truth_input)
    """
    In tensorflow, the confusion matrix is given as: predicted labels are along columns
    and true labels are along rows. Its a square matrix of size (label_count, label_count).
    """
    confusion_matrix = tf.confusion_matrix(
        ground_truth_input, predicted_indices, num_classes=label_count)
    
    # tf.summary.scalar('confusion_matrix', confusion_matrix)
    evaluation_step = tf.reduce_mean(tf.cast(correct_prediction, tf.float32))
    tf.summary.scalar('accuracy', evaluation_step)

    global_step = tf.train.get_or_create_global_step()
    increment_global_step = tf.assign(global_step, global_step + 1)

    saver = tf.train.Saver(tf.global_variables())

    # Merge all the summaries and write them out to /tmp/retrain_logs (by default)
    merged_summaries = tf.summary.merge_all()
    train_writer = tf.summary.FileWriter(FLAGS.summaries_dir + '/train',
                                        sess.graph)
    validation_writer = tf.summary.FileWriter(
        FLAGS.summaries_dir + '/validation')

    init = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
    sess.run(init)
    # tf.global_variables_initializer().run()

    start_step = 1

    if FLAGS.start_checkpoint:
        models.load_variables_from_checkpoint(sess, FLAGS.start_checkpoint)
        start_step = global_step.eval(session=sess)
        if FLAGS.set_prefilter:
            models.init_variables_from_checkpoint(
                None, FLAGS.set_prefilter, 'freqconv/')
    elif FLAGS.init_checkpoint:
        models.init_variables_from_checkpoint(sess, FLAGS.init_checkpoint)
    elif FLAGS.set_prefilter:
        models.init_variables_from_checkpoint(
            sess, FLAGS.set_prefilter, 'freqconv/')

    # Save graph.pbtxt.
    tf.train.write_graph(sess.graph_def, FLAGS.train_dir,
                         FLAGS.model_architecture + '_training.pbtxt')
    # sys.exit(0)

    # Save list of words.
    with gfile.GFile(os.path.join(FLAGS.train_dir, FLAGS.model_architecture + '_labels.txt'), 'w') as f:
        f.write('\n'.join(audio_processor.words_list))

    if FLAGS.training_mode:
        tf.logging.info('Training from step: %d ', start_step)

        """
        Switch on testng mode, if training a new model, thus making it default
        """        
        # FLAGS.testing_mode = True
        training_steps_max = np.sum(training_steps_list)
        for training_step in range(start_step, training_steps_max + 1):
            # Figure out what the current learning rate is.
            training_steps_sum = 0
            for i in range(len(training_steps_list)):
                training_steps_sum += training_steps_list[i]
                if training_step <= training_steps_sum:
                    learning_rate_value = learning_rates_list[i]
                    break
            # Pull the audio samples we'll use for training.
            train_audio, train_ground_truth = audio_processor.get_data(
                FLAGS.batch_size, 0, model_settings, FLAGS.background_frequency,
                FLAGS.background_volume, time_shift_samples, 'training', sess)
            # Run the graph with this batch of training data.
            train_summary, train_accuracy, cross_entropy_value, _, _, fingerprint_4d_val = sess.run(
                [merged_summaries, evaluation_step, cross_entropy_mean,
                    train_step, increment_global_step, fingerprint_4d],
                feed_dict={
                    audio_input: train_audio,
                    ground_truth_input: train_ground_truth,
                    learning_rate_input: learning_rate_value,
                    dropout_prob: 0.5
                })
            train_writer.add_summary(train_summary, training_step)
            tf.logging.info('Step #%d: rate %f, accuracy %.1f%%, cross entropy %f' %
                            (training_step, learning_rate_value, train_accuracy * 100,
                            cross_entropy_value))
            is_last_step = (training_step == training_steps_max)
            if (training_step % FLAGS.eval_step_interval) == 0 or is_last_step:
                set_size = audio_processor.set_size('validation')
                total_accuracy = 0
                total_conf_matrix = None
                for i in range(0, set_size, FLAGS.batch_size):
                    validation_audio, validation_ground_truth = (
                        audio_processor.get_data(FLAGS.batch_size, i, model_settings, 0.0,
                                                0.0, 0, 'validation', sess))
                    # Run a validation step and capture training summaries for TensorBoard
                    # with the `merged` op.
                    validation_summary, validation_accuracy, conf_matrix = sess.run(
                        [merged_summaries, evaluation_step, confusion_matrix],
                        feed_dict={
                            audio_input: validation_audio,
                            ground_truth_input: validation_ground_truth,
                            dropout_prob: 1.0
                        })
                    validation_writer.add_summary(validation_summary, training_step)
                    batch_size = min(FLAGS.batch_size, set_size - i)
                    total_accuracy += (validation_accuracy * batch_size) / set_size
                    if total_conf_matrix is None:
                        total_conf_matrix = conf_matrix
                    else:
                        total_conf_matrix += conf_matrix
                tf.logging.info('Confusion Matrix:\n %s' % (total_conf_matrix))
                tf.logging.info('Step %d: Validation accuracy = %.1f%% (N=%d)' %
                                (training_step, total_accuracy * 100, set_size))
                plot_confusion_matrix(total_conf_matrix, "Valid", audio_processor)

            # Save the model checkpoint periodically.
            if (training_step % FLAGS.save_step_interval == 0 or
                    training_step == training_steps_max):
                checkpoint_path = os.path.join(
                    FLAGS.train_dir, FLAGS.model_architecture + '.ckpt')
                tf.logging.info('Saving to "%s-%d"', checkpoint_path, training_step)
                saver.save(sess, checkpoint_path, global_step=training_step)

    # if FLAGS.testing_mode: ### and FLAGS.start_checkpoint is not None:
    #     set_size = audio_processor.set_size('validation')
    #     # tf.logging.info('set_size=%d', set_size)
    #     total_accuracy = 0
    #     total_conf_matrix = None
    #     ground_truth, predictions = [], []
    #     for i in range(0, set_size, FLAGS.batch_size):
    #         test_audio, test_ground_truth = audio_processor.get_data(
    #             FLAGS.batch_size, i, model_settings, 0.0, 0.0, 0, 'validation', sess)
    #         test_accuracy, conf_matrix, gt_input, preds = sess.run(
    #             [evaluation_step, confusion_matrix, ground_truth_input, predicted_indices],
    #             feed_dict={
    #                 audio_input: test_audio,
    #                 ground_truth_input: test_ground_truth,
    #                 dropout_prob: 1.0
    #             })
    #         batch_size = min(FLAGS.batch_size, set_size - i)
    #         total_accuracy += (test_accuracy * batch_size) / set_size
    #         if total_conf_matrix is None:
    #             total_conf_matrix = conf_matrix
    #         else:
    #             total_conf_matrix += conf_matrix
    #         """
    #         Note that confusion matrix, ground truth and predictions are output as numpy arrays
    #         """
    #         ground_truth.extend(list(gt_input))
    #         predictions.extend(list(preds))
    #     tf.logging.info('Confusion Matrix:\n %s' % (total_conf_matrix))
    #     tf.logging.info('Final test accuracy = %.1f%% (N=%d)' %
    #                     (total_accuracy * 100, set_size))
    #     plot_confusion_matrix(total_conf_matrix, "Test", audio_processor)
    #     classification_report(ground_truth, predictions, "Valid", audio_processor)
        

    if True:
        """
        defining a new loop to print confusion matrix and classification report 
        for all the dataset components, namely training, testing and validation.
        """
        for task in ["testing", "validation"]:
            set_size = audio_processor.set_size(task)
            total_accuracy = 0
            total_conf_matrix = None
            gnd_truth, predict = [], []
            """
            Parameters defining the noise amount and the number of samples corrupted by it.
            Time_shift_samples is kept fixed at 0.
            """
            expt_settings = {"background_frequency": 1.0, "background_volume": 1.0, "time_shift_samples": 0}
            """
            Save the experiment's settings in a csv file which will later contain the precision recall table
            """
            datasave = os.path.abspath(os.path.join(FLAGS.train_dir, "Saved_Data_Figures", "Trial_10"))
            os.makedirs(datasave, exist_ok=True)
            with open(os.path.join(datasave, "Parameter_settings_for_noise.txt"), 'w') as f:
                for key, val in expt_settings.items():
                    f.write(key + "\t" + str(val) + "\n")
            f.close()

            for i in range(0, set_size, FLAGS.batch_size):
                audio, ground_truth = audio_processor.get_data(
                    FLAGS.batch_size, i, model_settings, expt_settings["background_frequency"], \
                        expt_settings["background_volume"], expt_settings["time_shift_samples"], task, sess)
                accuracy, conf_matrix, gt_input, preds = sess.run(
                    [evaluation_step, confusion_matrix, ground_truth_input, predicted_indices],
                    feed_dict={
                        audio_input: audio,
                        ground_truth_input: ground_truth,
                        dropout_prob: 1.0
                    })
                batch_size = min(FLAGS.batch_size, set_size - i)
                total_accuracy += (accuracy * batch_size) / set_size
                if total_conf_matrix is None:
                    total_conf_matrix = conf_matrix
                else:
                    total_conf_matrix += conf_matrix
                """
                Note that confusion matrix, ground truth and predictions are output as numpy arrays
                """
                gnd_truth.extend(list(gt_input))
                predict.extend(list(preds))
            tf.logging.info('Confusion Matrix:\n %s' % (total_conf_matrix))
            tf.logging.info('Final %s accuracy = %.1f%% (N=%d)' %
                            (task, total_accuracy * 100, set_size))
            plot_confusion_matrix(total_conf_matrix, task, audio_processor, datasave)
            classification_report(gnd_truth, predict, task, audio_processor, datasave)



"""
Function to plot the confusion matrix for training/validation/testing dataset
"""
def plot_confusion_matrix(conf_mat, mode, audio_processor, figsave):
    sns_plot = sns.heatmap(conf_mat, annot=True, fmt="d", \
        cmap=sns.color_palette("Blues", n_colors=10, desat=0.7), linewidths=1, \
            cbar=False, annot_kws={"size": 13, 'weight':'bold'})
    sns_plot.set_xticklabels(audio_processor.words_list, fontdict={'fontsize':11, 'rotation': 45})
    sns_plot.set_yticklabels(audio_processor.words_list, fontdict={'fontsize':11, 'rotation': 45})
    sns_plot.set_xlabel("Predictions", fontsize=14)
    sns_plot.set_ylabel("Ground Truth", fontsize=14)
    sns_fig = sns_plot.get_figure()
    sns_fig.tight_layout()
    sns_fig.savefig(os.path.join(figsave, "Final_Confusion_Matrix_" + mode.upper() + ".png"), dpi=300)
    sns_plot.get_figure().clf()

    """
    Save the confusion matrix
    True value are along the rows, the predictions are along columns.
    """
    conf_mat_df = pd.DataFrame(conf_mat, columns=audio_processor.words_list)
    conf_mat_df.index = audio_processor.words_list
    conf_mat_df.to_csv(os.path.join(figsave, "Final_Confusion_Matrix_" + mode.upper() + ".csv"))


def classification_report(ground_truth, predicts, mode, audio_processor, datasave):
    class_report_df = pd.DataFrame(classreport(ground_truth, predicts, digits=2, \
        target_names=audio_processor.words_list, output_dict=True)).transpose()
    """Discard the rows with minor and macro averaging"""
    class_report_df = class_report_df[:len(audio_processor.words_list)]
    """Round of decimal places to 2"""
    class_report_df = class_report_df.apply(lambda x: round(x, 2), axis=0)
    print(class_report_df)
    class_report_df.to_csv(os.path.join(datasave, \
        "Precision_Recall_Class_wise_" + mode.upper() + "_data.csv"))    




if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--data_url',
        type=str,
        # pylint: disable=line-too-long
        # default='http://download.tensorflow.org/data/speech_commands_v0.01.tar.gz',
        default='',
        # pylint: enable=line-too-long
        help='Location of speech training data archive on the web.')
    parser.add_argument(
        '--data_dir',
        type=str,
        default='/tmp/speech_dataset/',
        help="""\
        Where to download the speech training data to.
        """)
    parser.add_argument(
        '--training_mode',
        type=bool,
        default=True,
        help="""\
        Do you want to train a model?
        """)
    parser.add_argument(
        '--testing_mode',
        type=bool,
        default=True,
        help="""\
        Do you want to just check confusion matrix?
        """)
    parser.add_argument(
        '--background_volume',
        type=float,
        default=0.1,
        help="""\
        How loud the background noise should be, between 0 and 1.
        """)
    parser.add_argument(
        '--background_frequency',
        type=float,
        default=0.8,
        help="""\
        How many of the training samples have background noise mixed in.
        """)
    parser.add_argument(
        '--silence_percentage',
        type=float,
        default=10.0,
        help="""\
        How much of the training data should be silence.
        """)
    parser.add_argument(
        '--unknown_percentage',
        type=float,
        default=10.0,
        help="""\
        How much of the training data should be unknown words.
        """)
    parser.add_argument(
        '--time_shift_ms',
        type=float,
        default=100.0,
        help="""\
        Range to randomly shift the training audio by in time.
        """)
    parser.add_argument(
        '--testing_percentage',
        type=int,
        default=10,
        help='What percentage of wavs to use as a test set.')
    parser.add_argument(
        '--validation_percentage',
        type=int,
        default=10,
        help='What percentage of wavs to use as a validation set.')
    parser.add_argument(
        '--sample_rate',
        type=int,
        default=8000,
        help='Expected sample rate of the wavs',)
    parser.add_argument(
        '--clip_duration_ms',
        type=int,
        default=1000,
        help='Expected duration in milliseconds of the wavs',)
    parser.add_argument(
        '--window_size_ms',
        type=float,
        default=30.0,
        help='How long each spectrogram timeslice is',)
    parser.add_argument(
        '--window_stride_ms',
        type=float,
        default=10.0,
        help='How long each spectrogram timeslice is',)
    parser.add_argument(
        '--dct_coefficient_count',
        type=int,
        default=64,
        help='How many bins to use for the MFCC fingerprint',)
    parser.add_argument(
        '--how_many_training_steps',
        type=str,
        default='15000,3000',
        help='How many training loops to run',)
    parser.add_argument(
        '--eval_step_interval',
        type=int,
        default=400,
        help='How often to evaluate the training results.')
    parser.add_argument(
        '--learning_rate',
        type=str,
        default='0.001,0.0001',
        help='How large a learning rate to use when training.')
    parser.add_argument(
        '--batch_size',
        type=int,
        default=128,
        help='How many items to train with at once',)
    parser.add_argument(
        '--summaries_dir',
        type=str,
        default='/tmp/retrain_logs',
        help='Where to save summary logs for TensorBoard.')
    parser.add_argument(
        '--wanted_words',
        type=str,
        default='yes,no,up,down,left,right,on,off,stop,go',
        help='Words to use (others will be added to an unknown label)',)
    parser.add_argument(
        '--train_dir',
        type=str,
        default='/tmp/speech_commands_train',
        help='Directory to write event logs and checkpoint.')
    parser.add_argument(
        '--save_step_interval',
        type=int,
        default=300,
        help='Save model checkpoint every save_steps.')
    parser.add_argument(
        '--start_checkpoint',
        type=str,
        default='',
        help='If specified, restore this pretrained model before any training.')
    parser.add_argument(
        '--model_architecture',
        type=str,
        default='conv',
        help='What model architecture to use')
    parser.add_argument(
        '--check_nans',
        type=bool,
        default=False,
        help='Whether to check for invalid numbers during processing')
    parser.add_argument(
        '--optimizer',
        type=str,
        default='SGD',
        help='What optimizer architecture to use. SGD or Adam')
    parser.add_argument(
        '--init_checkpoint',
        type=str,
        default='',
        help='If specified, restore this pretrained model before any training.')
    parser.add_argument(
        '--set_prefilter',
        type=str,
        default='',
        help='If specified, load prefilter value.')
    parser.add_argument(
        '--lock_prefilter',
        dest='lock_prefilter',
        action='store_true',
        help='Whether to lock prefilter weights in training')
    parser.add_argument(
        '--no_prefilter_bias',
        dest='prefilter_bias',
        action='store_false',
        help='Do not add bias on prefilter')
    parser.add_argument(
        '--downsample',
        type=int,
        default=1,
        help='Downsample.')
    parser.add_argument(
        '--use_down_avgfilt',
        dest='use_down_avgfilt',
        action='store_true',
        help='Whether to use average filter in prefilter downsampling')
    parser.add_argument(
        '--norm_binw',
        dest='norm_binw',
        action='store_true',
        help='Whether to normalize binary convolution weights')
    parser.add_argument(
        '--freeze',
        default=False,
        action='store_true',
        help="Flag to genrate pb file from latest checkpoint")

    FLAGS, unparsed = parser.parse_known_args()
    tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)