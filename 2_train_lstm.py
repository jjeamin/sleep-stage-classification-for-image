import json
import torch
import torch.nn as nn
import timm
from torchvision import transforms
from torch.utils.data import DataLoader
from sklearn.model_selection import train_test_split
from models import SleepStager
from utils import *

RANDOM_STATE = 777
torch.manual_seed(RANDOM_STATE)

IMAGE_SIZE = 224
BATCH_SIZE = 1
LR_START = 0.1
EPOCHS = 10
DEVICE = 'cuda'
N_SPLIT = 5
SEQ_LEN = 5
NUM_WORKER = 8

DATA_PATH = 'data/PSG'
JSON_PATH = 'data/PSG.json'
CHECKPOINT_PATH = 'checkpoints'
LOG_PATH = 'lstm.log'

CONV_PATH = 'checkpoints/resnet101_224_fold.pth'
SAVE_PATH = 'checkpoints/resnet101_lstm_224_fold.pth'

LABEL_NAME = ['Wake', 'N1', 'N2', 'N3', 'REM']

logger = get_logger(LOG_PATH, 'train')

train_transforms = transforms.Compose([
    transforms.Grayscale(),
    transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(0.5, 0.5)
])

valid_transforms = transforms.Compose([
    transforms.Grayscale(),
    transforms.Resize((IMAGE_SIZE, IMAGE_SIZE)),
    transforms.ToTensor(),
    transforms.Normalize(0.5, 0.5)
])

with open(JSON_PATH, 'r') as f:
    json_data = json.load(f)

patients = json_data['Patient']

train_patients, test_patients = train_test_split(
    patients, test_size=0.1, random_state=RANDOM_STATE)
train_patients, valid_patients = train_test_split(
    train_patients, test_size=0.11, random_state=RANDOM_STATE)

train_dataset = SleepSeqDataset(
    train_patients, DATA_PATH, LABEL_NAME, SEQ_LEN, train_transforms)
valid_dataset = SleepSeqDataset(
    valid_patients, DATA_PATH, LABEL_NAME, SEQ_LEN, valid_transforms)

train_loader = DataLoader(train_dataset,
                          batch_size=BATCH_SIZE,
                          num_workers=NUM_WORKER,
                          pin_memory=True,
                          shuffle=True)

valid_loader = DataLoader(valid_dataset,
                          batch_size=BATCH_SIZE,
                          num_workers=NUM_WORKER,
                          pin_memory=True,
                          shuffle=False)

# conv
encoder = timm.models.resnet101(pretrained=False)
encoder.conv1 = nn.Conv2d(1, 64, kernel_size=(
    7, 7), stride=(2, 2), padding=(3, 3), bias=False)
encoder.fc = nn.Linear(
    in_features=2048, out_features=len(LABEL_NAME), bias=True)
encoder = encoder.to(DEVICE)
encoder = nn.DataParallel(encoder)
encoder.load_state_dict(torch.load(CONV_PATH))
encoder.module.fc = nn.Identity()

# freeze
for name, child in encoder.named_children():
    for param in child.parameters():
        param.requires_grad = False

# lstm
model = SleepStager(encoder, SEQ_LEN, num_classes=len(LABEL_NAME))
model.encoder = model.encoder.module
model = model.to(DEVICE)
model = nn.DataParallel(model)

train_total = len(train_dataset)
valid_total = len(valid_dataset)

early_stopping = EarlyStopping(verbose=True, path=SAVE_PATH)
criterion = LabelSmoothingCrossEntropy().to(DEVICE)
optimizer = torch.optim.SGD(
    model.parameters(), lr=LR_START, weight_decay=1e-5, momentum=0.9)
scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
    optimizer, T_0=EPOCHS + 1, T_mult=2, eta_min=0.0001, last_epoch=-1)

# train loop
for e in range(0, EPOCHS):
    train_correct, train_loss = train_lstm(
        model, train_loader, optimizer, criterion, device=DEVICE)
    train_acc = train_correct / train_total
    train_loss = train_loss / train_total

    valid_correct, valid_loss = valid_lstm(
        model, valid_loader, criterion, device=DEVICE)
    valid_acc = valid_correct / valid_total
    valid_loss = valid_loss / valid_total

    scheduler.step()

    logger.info(
        "===============================================================")
    logger.info(
        "===============================================================")
    logger.info(f"||    EPOCH : {EPOCHS} / {e}]   ||")
    logger.info(
        f"|| [TRAIN ACC : {train_acc}] || [TRAIN LOSS : {train_loss}] ||")
    logger.info(
        f"|| [VALID ACC : {valid_acc}] || [VALID LOSS : {valid_loss}] ||")
    logger.info(
        "===============================================================")
    logger.info(
        "===============================================================")

    early_stopping(valid_loss, model)

    if early_stopping.early_stop:
        logger.info("Earlyt- stopping")
        break

    model.load_state_dict(torch.load(SAVE_PATH))
