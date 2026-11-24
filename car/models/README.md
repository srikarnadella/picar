# Models

`detect.tflite` + `labelmap.txt`: COCO SSD-MobileNet v1, quantized (TensorFlow detection model zoo, Apache 2.0),
from https://storage.googleapis.com/download.tensorflow.org/models/tflite/coco_ssd_mobilenet_v1_1.0_quant_2018_06_29.zip.
Measured on the car (Pi 3B, tflite-runtime 2.11, 2 threads): 231 ms per frame, about 4 fps, 58 MB.
