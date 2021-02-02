#!/usr/bin/env python


# Load the necessary python libraries
import random
from threading import Lock
from shutil import copy
import numpy as np
import librosa
import os
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm

from flask import Flask, render_template, session, request, \
    copy_current_request_context
from flask_socketio import SocketIO, emit, join_room, leave_room, \
    close_room, rooms, disconnect
from scipy.io.wavfile import write
from main import personalize_model, predict_query, ProtoSoundDataset

# Set this variable to "threading", "eventlet" or "gevent" to test the
# different async modes, or leave it set to None for the application to choose
# the best option based on installed packages.

async_mode = None

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, async_mode=async_mode)
thread = None
thread_lock = Lock()

RATE = 44100
# VARIABLES
WAYS = 5
SHOTS = 5
LIBRARY_DATA_PATH = 'library'
DATA_PATH = 'meta-test-data'  # DIRECTORY OF 25 SAMPLES
DATA_CSV_PATH = ''
MODEL_PATH = 'model/protosound_10_classes.pt'
QUERY_PATH = 'meta-test-query/'
QUERY_FILE = 'meta-test-data/kitchen_hazard-alarm_5ft_sample4_344_chunk0_009.wav'
user_prototype_available = False

# GET DEVICE
if torch.cuda.is_available():
    device = torch.device('cuda:0')
else:
    device = torch.device('cpu')

# GET MODEL
protosound_model = torch.load(MODEL_PATH, map_location=device)
protosound_model = protosound_model.to(device)
classes_prototypes = None  # TODO: Replace this variable once the training is done
support_data = None

"""
    Flask SocketIO functions
"""


def seed():
    return 0.31415926


def background_thread():
    """Example of how to send server generated events to clients."""
    count = 0
    while True:
        socketio.sleep(10)
        count += 1
        socketio.emit('my_response',
                      {'data': 'Server generated event', 'count': count})


def seed():
    return 0.31415926


def background_thread():
    """Example of how to send server generated events to clients."""
    count = 0
    while True:
        socketio.sleep(10)
        count += 1
        socketio.emit('my_response',
                      {'data': 'Server generated event', 'count': count})


def add_background_noise(input_data, noise_data, noise_ratio=0.5):
    output = input_data * (1 - noise_ratio) + noise_data * noise_ratio
    output = output.astype(np.float32)
    return output


def generate_csv(data_path_directory, labels, output_path_directory):
    # dirs = sorted([dI for dI in os.listdir(data_path_directory) if os.path.isdir(os.path.join(data_path_directory, dI))])
    categories = labels
    c2i = {}
    i2c = {}
    for i, category in enumerate(categories):
        c2i[category] = i
        i2c[i] = category

    name = []
    fold = []
    category = []
    for ea_dir in labels:
        csv_index = 1
        path = os.path.join(data_path_directory, ea_dir)
        file_list = os.listdir(path)
        random.shuffle(file_list, seed)
        for file_name in file_list:
            try:
                if file_name.endswith('.wav'):
                    file_path = os.path.join(path, file_name)
                    if not os.path.exists(output_path_directory):
                        os.makedirs(output_path_directory)
                    category.append(ea_dir)
                    name.append(file_name)
                    # index = num_class + 1
                    if csv_index == len(labels) + 1:
                        csv_index = 1
                    fold.append(csv_index)
                    csv_index += 1
                    # Copy filepath to output_path_directory
                    copy(file_path, output_path_directory)
            except:
                open("exceptions.txt", "a").write("Exception raised for: %s\n" % file_name)
    dict = {'filename': name, 'fold': fold, 'category': category}
    df = pd.DataFrame(dict)
    df.to_csv(output_path_directory + '/user_data.csv')
    global DATA_CSV_PATH
    DATA_CSV_PATH = output_path_directory + '/user_data.csv';
    print("Generata CSV complete.")


@socketio.on('submit_data')
def submit_data(json_data):
    print("submit_data->receive request")
    labels = json_data['label']
    submitAudioTime = str(json_data['submitAudioTime'])
    labels = [element.lower().replace(" ", "_") for element in labels]
    background_noise = np.asarray(json_data['data_15'], dtype=np.int16) / 32768.0
    background_noise = background_noise[:44100]
    for i in range(0, 15):
        # generate new directory for new data, store it into "library" for future usage
        label = labels[i // 5]
        current_dir = os.path.join(LIBRARY_DATA_PATH, label)
        if not os.path.exists(current_dir):
            os.makedirs(current_dir)
        data = json_data['data_' + str(i)]
        np_data = np.asarray(data, dtype=np.int16) / 32768.0
        np_data = np_data[:44100]
        output = add_background_noise(np_data, background_noise, 0.25)

        filename = os.path.join(current_dir, label + "_user_" + str(i % 5) + '.wav')

        write(filename, RATE, output)
    
    # Generate CSV file and put 25 samples into one single folder
    print('Generate CSV file and put 25 samples into one single folder')
    generate_csv(LIBRARY_DATA_PATH, labels, DATA_PATH)

    # LOAD DATA
    df = pd.read_csv(DATA_CSV_PATH)
    global support_data
    support_data = ProtoSoundDataset(DATA_PATH, df, 'filename', 'category')
    print(support_data.c2i, support_data.categories, support_data.i2c)
    # TRAIN MODEL   
    train_loader = DataLoader(support_data, batch_size=WAYS*SHOTS)
    batch = next(iter(train_loader))
    # IMPORTANT: MAKE SURE THAT "classes_prototypes" AND "support_data.i2c" 
    # ARE GLOBAL VARIABLEs WHICH NEED TO CHANGE EVERY TIME TRAINING IS DONE
    global classes_prototypes
    classes_prototypes = personalize_model(protosound_model, batch, WAYS, SHOTS, device=device)
    print("training complete")
    socketio.emit('training_complete', { 'submitAudioTime': submitAudioTime })


@socketio.on('audio_data_c2s')
def handle_source(json_data):
    data = np.asarray(json_data['data'], dtype=np.int16) / 32768.0
    db = json_data['db']
    db = str(round(db, 2))

    recordTime = str(json_data['record_time'])
    print("db:", db)
    data = data[:44100]
    PREDICTION_QUERY_FILE_NAME = 'query'
    # Write the prediction query file
    QUERY_FILE = QUERY_PATH + '/' + PREDICTION_QUERY_FILE_NAME + '.wav'
    write(QUERY_FILE, RATE, data)
    # Make prediction
    global support_data
    global classes_prototypes
    if support_data is None or classes_prototypes is None:
        print('no training happened yet')
        return
    output, confidence = predict_query(protosound_model, QUERY_FILE, classes_prototypes, support_data.i2c, device=device)
    print('output:', output[0], 'confidence', confidence[0])
    if (confidence < -10):
        print("Exit due to confidence < -10: ")
        return

    print('Making prediction...', output[0])
    socketio.emit('audio_data_s2c', {
        'label': output[0],
        'confidence': str(confidence[0]),
        'db': db,
        'recordTime': recordTime # pass the record time back if exist
        })


@app.route('/')
def index():
    return render_template('index.html', async_mode=socketio.async_mode)


@socketio.on('disconnect')
def test_disconnect():
    print('Client disconnected', request.sid)


@socketio.on('connect')
def test_connect():
    print('Client connect', request.sid)
    global support_data
    global classes_prototypes
    global user_prototype_available
    if (support_data is not None and classes_prototypes is not None):
        user_prototype_available = True
    socketio.emit("server_received_request", {
        'user_prototype_available': user_prototype_available
    })


if __name__ == '__main__':
    socketio.run(app, host='128.208.49.41', port=5000, debug=True)
