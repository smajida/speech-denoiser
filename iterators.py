import librosa
import numpy as np
from params import *
from multiprocessing import Process, Queue, JoinableQueue, Value
from threading import Thread
import theano
from time import time
import socket
import pickle


class ParallelBatchIterator(object):
    """
    Uses a producer-consumer model to prepare batches on the CPU while training on the GPU.
    """

    def __init__(self, X, y, batch_size, dataset, shuffle=False, preprocess=False):
        self.batch_size = batch_size
        self.X = X
        self.y = y
        self.dataset = dataset
        self.shuffle = shuffle
        np.random.seed(0)
        if preprocess:
            self.pre_process()

    def chunks(self, l, n):
        """ Yield successive n-sized chunks from l.
            from http://goo.gl/DZNhk
        """
        for i in xrange(0, len(l), n):
            yield l[i:i + n]

    def read_data(self, filename):
        with open(filename, 'rb') as f:
            data = np.fromfile(f, dtype='>i2')

        #TODO: different normalization?
        return data / (0.001 + np.max(np.abs(data)))

    def process(self, key_x, key_y, path='aurora2/'):
        # Read X
        x = self.read_data(path + self.dataset + '/' + key_x)

        # Read Y
        y = self.read_data(path + self.dataset + '/' + key_y)

        return x, y

    def process_temp(self, key_x, key_y, path='aurora2/'):
        with open(path + self.dataset + '/' + key_x+'.npy', 'rb') as f:
            x = np.load(f)
        with open(path + self.dataset + '/' + key_y+'.npy', 'rb') as f:
            y = np.load(f)
        return x, y

    def gen(self, indices, temp=True):
        key_batch_x = [self.X[ix] for ix in indices]
        key_batch_y = [self.y[ix] for ix in indices]

        cur_batch_size = len(indices)

        if not temp:
            X_batch = np.zeros((cur_batch_size, params.MAX_LENGTH), dtype=theano.config.floatX)
            y_batch = np.zeros((cur_batch_size, params.MAX_LENGTH), dtype=theano.config.floatX)
        else:
            hop_length = (params.STEP_SIZE / 1000.0) * params.SR
            X_batch_new = np.zeros((cur_batch_size, params.N_COMPONENTS, int(params.MAX_LENGTH/hop_length)), dtype=theano.config.floatX)
            y_batch_new = np.zeros_like(X_batch_new)

        # Read all images in the batch
        for i in range(len(key_batch_x)):
            #TODO: find MAX_LENGTH
            if temp:
                X, y = self.process_temp(key_batch_x[i], key_batch_y[i])
                X_batch_new[i] = X
                y_batch_new[i] = y
            else:
                X, y = self.process(key_batch_x[i], key_batch_y[i])
                X_batch[i, :X.shape[0]], y_batch[i, :y.shape[0]] = X[:X_batch.shape[1]], y[:y_batch.shape[1]]

        # Transform the batch (augmentation, fft, normalization, etc.)
        if not temp:
            X_batch_new, y_batch_new = self.transform(X_batch, y_batch, sr=params.SR)

        return X_batch_new, y_batch_new, key_batch_x

    def __iter__(self):
        queue = JoinableQueue(maxsize=params.N_PRODUCERS * 8)

        n_batches, job_queue = self.start_producers(queue)

        # Run as consumer (read items from queue, in current thread)
        for x in xrange(n_batches):
            item = queue.get()
            yield item
            queue.task_done()

        queue.close()
        job_queue.close()
        if self.shuffle:
            shuffled_idx = np.random.permutation(len(self.X))
            X_new = []
            y_new = []
            for i in range(len(self.X)):
                X_new += [self.X[shuffled_idx[i]]]
                y_new += [self.y[shuffled_idx[i]]]
            self.X = X_new
            self.y = y_new

    def start_producers(self, result_queue):
        jobs = Queue()
        n_workers = params.N_PRODUCERS
        batch_count = 0

        # Flag used for keeping values in queue in order
        last_queued_job = Value('i', -1)

        for job_index, batch in enumerate(self.chunks(range(0, len(self.X)), self.batch_size)):
            batch_count += 1
            jobs.put((job_index, batch))

        # Define producer (putting items into queue)
        def produce(id):
            while True:
                job_index, task = jobs.get()

                if task is None:
                    break

                result = self.gen(task)

                while(True):
                    # My turn to add job done
                    if last_queued_job.value == job_index - 1:
                        with last_queued_job.get_lock():
                            result_queue.put(result)
                            last_queued_job.value += 1
                            break

        # Start workers
        for i in xrange(n_workers):
            if params.MULTIPROCESS:
                p = Process(target=produce, args=(i,))
            else:
                p = Thread(target=produce, args=(i,))

            p.daemon = True
            p.start()

        # Add poison pills to queue (to signal workers to stop)
        for i in xrange(n_workers):
            jobs.put((-1, None))

        return batch_count, jobs

    def transform(self, Xb, yb, sr):
        n_fft = self.next_greater_power_of_2((params.WINDOW_SIZE/1000.0) * params.SR)
        hop_length = int((params.STEP_SIZE / 1000.0) * params.SR)
        Xb_new = np.zeros((Xb.shape[0], params.N_COMPONENTS, params.MAX_LENGTH/hop_length), dtype=theano.config.floatX)
        yb_new = np.zeros_like(Xb_new)
        #TODO: preprocess and load instead of transforming each time.
        for i in range(Xb.shape[0]):
            if params.MFCC:
                Xb_new[i] = librosa.feature.mfcc(Xb[i], sr, n_mfcc=params.N_COMPONENTS, n_fft=n_fft, hop_length=hop_length, S=None)[:,:-1]
                yb_new[i] = librosa.feature.mfcc(yb[i], sr, n_mfcc=params.N_COMPONENTS, n_fft=n_fft, hop_length=hop_length, S=None)[:,:-1]
            else:
                Xb_new[i] = librosa.feature.melspectrogram(Xb[i], sr, n_mels=params.N_COMPONENTS, n_fft=n_fft, hop_length=hop_length)[:,:-1]
                yb_new[i] = librosa.feature.melspectrogram(yb[i], sr, n_mels=params.N_COMPONENTS, n_fft=n_fft, hop_length=hop_length)[:,:-1]
            Xb_new[i] /= np.max(Xb_new[i])+1.e-12
            yb_new[i] /= np.max(yb_new[i])+1.e-12
        return Xb_new, yb_new


    def pre_process(self, path='aurora2/'):
        for i in range(len(self.X)):
            print('preprocessing ', i)
            key_x, key_y = self.X[i], self.y[i]
            x_raw, y_raw = self.process(key_x, key_y)
            x = np.zeros((1, params.MAX_LENGTH), dtype=theano.config.floatX)
            y = np.zeros_like(x)
            start = 0
            end = x_raw.shape[0]
            if x_raw.shape[0] > x.shape[1]:
                #take middle
                start = (x_raw.shape[0] - x.shape[1])/2
                end = start + x.shape[1]
            x[0, :x_raw.shape[0]] = x_raw[start:end]
            y[0, :y_raw.shape[0]] = y_raw[start:end]
            x_new, y_new = self.transform(x, y, params.SR)
            with open(path + self.dataset + '/'+key_x+'.npy', 'wb') as f:
                np.save(f, x_new)
            with open(path + self.dataset + '/'+key_y+'.npy', 'wb') as f:
                np.save(f, y_new)

    def next_greater_power_of_2(self, x):
        return int(2**np.math.ceil(np.math.log(x, 2)))

