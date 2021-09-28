import logging
import base64
import time
import json
import pickle
import time
import os
from datetime import datetime

import requests
import pushover
from fastapi import FastAPI
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%m/%d/%Y %I:%M:%S %p')
logging.info('App Started')
app = FastAPI()

po_client = pushover.Pushover()

with open('config/cameras.json') as f:
    cameradata = json.load(f)

with open('config/settings.json') as f:
    settings = json.load(f)

sssUrl = settings.get("sssUrl")
deepstackUrl = settings.get("deepstackUrl")
homebridgeWebhookUrl = settings.get("homebridgeWebhookUrl")
username = settings.get("username")
password = settings.get("password")
verify_tls = settings.get("verify_tls")

# If verify_tls isn't in json or is a blank string, verify by default
if verify_tls is None or not isinstance(verify_tls, bool):
    verify_tls = True

detection_labels = ['car', 'person']
if "detect_labels" in settings:
    detection_labels = settings["detect_labels"]

timeout = 10
if "timeout" in settings:
    timeout = int(settings["timeout"])

min_sizex = 0
if "min_sizex" in settings:
    min_sizex = int(settings["min_sizex"])

min_sizey = 0
if "min_sizey" in settings:
    min_sizey = int(settings["min_sizey"])

min_confidence = 0
if "min_confidence" in settings:
    min_confidence = int(settings["min_confidence"])

# If no trigger interval set then make it 60s (i.e. don't send another event from the triggered camera for at least 60s to stop flooding event notifications
trigger_interval = 60
if "triggerInterval" in settings:
    trigger_interval = settings["triggerInterval"]

capture_dir = "/captureDir"
if "captureDir" in settings:
    capture_dir = settings["captureDir"]

# Create a session with synology
url = f"{sssUrl}/webapi/auth.cgi?api=SYNO.API.Auth&method=Login&version=1&account={username}&passwd={password}&session=SurveillanceStation"

#  Save cookies
logging.info('Session login: ' + url)
session = requests.Session()
auth = session.get(url, verify=verify_tls)

# Dictionary to save last trigger times for camera to stop flooding the capability
last_trigger_fn = f"/tmp/last.dict"


def save_last_trigger(last_trigger):
    with open(last_trigger_fn, 'wb') as f:
        pickle.dump(last_trigger, f)


def load_last_trigger():
    if os.path.exists(last_trigger_fn):
        with open(last_trigger_fn, 'rb') as f:
            return pickle.load(f)
    else:
        return {}


def contains(rOutside, rInside):
    return rOutside["x_min"] < rInside["x_min"] < rInside["x_max"] < rOutside["x_max"] and \
        rOutside["y_min"] < rInside["y_min"] < rInside["y_max"] < rOutside["y_max"]


# If you would like to ignore objects outside the ignore area instead of inside, set this to contains(rect, ignore_area):
def isIgnored(rect, ignore_areas):
    for ignore_area in ignore_areas:
        if contains(ignore_area, rect):
            logging.info('Object in ignore area, not triggering')
            return True
    return False


@app.get("/{camera_id}")
async def read_item(camera_id):
    start = time.time()
    cameraname = cameradata[f"{camera_id}"]["name"]
    predictions = None
    last_trigger = load_last_trigger()

    # Check we are outside the trigger interval for this camera
    if camera_id in last_trigger:
        t = last_trigger[camera_id]
        dt = datetime.fromtimestamp(t)
        logging.info(f"Found last time for {cameraname} was {dt}")
        if (start - t) < trigger_interval:
            msg = f"Skipping detection on {cameraname} since it was" \
                  f" only triggered {round(start - t, 1)}s ago"
            logging.info(msg)
            return (msg)
        else:
            logging.info(f"Processing event on {cameraname}"
                         f" (last trigger"
                         f" was {round(start-t, 1)}s ago)")
    else:
        logging.info(f"No last camera time for {cameraname}")

    url = f"{sssUrl}/webapi/entry.cgi?camStm=1&version=2&cameraId={camera_id}&api=%22SYNO.SurveillanceStation.Camera%22&method=GetSnapshot"
    triggerurl = cameradata[f"{camera_id}"]["triggerUrl"]
    if "homekitAccId" in cameradata[f"{camera_id}"]:
        homekit_acc_id = cameradata[f"{camera_id}"]["homekitAccId"]

    ignore_areas = []
    if "ignore_areas" in cameradata[f"{camera_id}"]:
        for ignore_area in cameradata[f"{camera_id}"]["ignore_areas"]:
            ignore_areas.append({
                "y_min": int(ignore_area["y_min"]),
                "x_min": int(ignore_area["x_min"]),
                "y_max": int(ignore_area["y_max"]),
                "x_max": int(ignore_area["x_max"])
            })

    logging.debug('Requesting snapshot: ' + url)
    response = session.get(url, verify=verify_tls)

    if response.status_code == 200:
        with open(f"/tmp/{camera_id}.jpg", 'wb') as f:
            f.write(response.content)
            logging.debug('Snapshot downloaded')

    snapshot_file = f"/tmp/{camera_id}.jpg"
    image_data = open(snapshot_file, "rb").read()
    logging.info('Requesting detection from DeepStack...')
    s = time.perf_counter()
    response = requests.post(f"{deepstackUrl}/v1/vision/detection", files={"image": image_data}, timeout=timeout, verify=verify_tls).json()

    e = time.perf_counter()
    logging.debug(f'Got result: {json.dumps(response, indent=2)}. Time: {e-s}s')
    if not response["success"]:
        return ("Error calling Deepstack: " + response["error"])

    labels = ''
    predictions = response["predictions"]
    for object in predictions:
        label = object["label"]
        if label != 'person':
            labels = labels + label + ' '

    i = 0
    found = False
    items_found = []
    for prediction in response["predictions"]:
        confidence = round(100 * prediction["confidence"])
        label = prediction["label"]
        sizex = int(prediction["x_max"])-int(prediction["x_min"])
        sizey = int(prediction["y_max"])-int(prediction["y_min"])
        item_string = f"Object: {label} - Confidence: {confidence}%" \
                      f" Size: {sizex}x{sizey}" \
                      f" X-Bounds: {prediction['x_min']}/{prediction['x_max']}" \
                      f" Y-Bounds: {prediction['y_min']}/{prediction['y_max']}"
        items_found.append(item_string)
        logging.info(f"  {item_string}")

        if not found and label in detection_labels and \
           sizex > min_sizex and \
           sizey > min_sizey and \
           confidence > min_confidence and \
           not isIgnored(prediction, ignore_areas):

            payload = {}
            logging.info(f"{confidence}% sure we found a {label}"
                         f" - triggering {cameraname} via request to"
                         f" the camera's webhook...")
            response = requests.get(triggerurl, data=payload, verify=verify_tls)
            end = time.time()
            runtime = round(end - start, 1)
            logging.info(f"Process duration: {runtime} seconds")

            found = True
            last_trigger[camera_id] = time.time()
            save_last_trigger(last_trigger)
            logging.debug(f"Saving last camera time for {camera_id} as {last_trigger[camera_id]}")

            if homebridgeWebhookUrl is not None and homekit_acc_id is not None:
                hb = requests.get(
                    f"{homebridgeWebhookUrl}/?accessoryId={homekit_acc_id}&state=true",
                    verify=verify_tls)
                logging.debug(f"Sent message to homebridge webhook: {hb.status_code}")
            else:
                logging.debug(f"Skipping HomeBridge Webhook since no webhookUrl or accessory Id")
        i += 1

    end = time.time()
    runtime = round(end - start, 1)
    if found:
        file_name = save_image(predictions, cameraname, snapshot_file, ignore_areas)
        pushover_message = f"Found object(s) on camera {cameraname}:" \
                           f"{os.linesep}" \
                           f"{os.linesep+os.linesep.join(items_found)} "
        logging.debug(f"Sending pushover message: {pushover_message}")
        with open(file_name, "r+b") as file:
            po_client.message(pushover_message,
                              title=f"Motion Dected on {cameraname}",
                              attachment=file)
        return "Triggering camera because something was found - took {runtime} seconds"
    else:
        logging.info(f"{cameraname} triggered - nothing found - took {runtime} seconds")
        return f"{cameraname} triggered - nothing found"


def save_image(predictions, camera_name, snapshot_file, ignore_areas):
    start = time.time()
    logging.debug(f"Saving new image file....")
    im = Image.open(snapshot_file)
    draw = ImageDraw.Draw(im)
    font = ImageFont.truetype("fonts/Gidole-Regular.ttf", size=40)

    for object in predictions:
        confidence = round(100 * object["confidence"])
        label = f"{object['label']} ({confidence}%)"
        draw.rectangle((object["x_min"], object["y_min"], object["x_max"],
                        object["y_max"]), outline=(192, 47, 29), width=2)
        draw.text((object["x_min"]+10, object["y_min"]+10),
                  f"{label}", fill=(192, 47, 29), font=font)

    for ignore_area in ignore_areas:
        draw.rectangle((ignore_area["x_min"], ignore_area["y_min"],
                        ignore_area["x_max"], ignore_area["y_max"]), outline=(255, 66, 66), width=2)
        draw.text((ignore_area["x_min"]+10, ignore_area["y_min"]+10), f"ignore", fill=(255, 66, 66))

    fn = f"{capture_dir}/{camera_name}-{start}.jpg"
    im.save(f"{fn}", quality=100)
    im.close()
    end = time.time()
    runtime = round(end - start, 1)
    logging.debug(f"Saved captured and annotated image: {fn} in {runtime} seconds.")
    
    return fn
