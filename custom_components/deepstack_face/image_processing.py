"""
Component that will perform facial recognition via deepstack.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/image_processing.deepstack_face
"""
import io
import logging
import re
import time
import os
from pathlib import Path
from typing import Optional


from PIL import Image, ImageDraw

import deepstack.core as ds
import homeassistant.helpers.config_validation as cv
from homeassistant.util.pil import draw_box
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.image_processing import (
    ATTR_CONFIDENCE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    PLATFORM_SCHEMA,
    ImageProcessingFaceEntity,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_IP_ADDRESS,
    CONF_PORT,
    CONF_NAME,
)
from homeassistant.core import split_entity_id
from homeassistant.helpers.reload import setup_reload_service


_LOGGER = logging.getLogger(__name__)

# rgb(red, green, blue)
RED = (255, 0, 0)  # For objects within the ROI

CONF_API_KEY = "api_key"
CONF_TIMEOUT = "timeout"
CONF_DETECT_ONLY = "detect_only"
CONF_SAVE_FILE_FOLDER = "save_file_folder"
CONF_SAVE_TIMESTAMPTED_FILE = "save_timestamped_file"
CONF_SAVE_FACES_FOLDER = "save_faces_folder"
CONF_PREVIEW_FACES_FOLDER = "preview_faces_folder"
CONF_SAVE_FACES = "save_faces"
CONF_SHOW_BOXES = "show_boxes"

DATETIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
DEFAULT_API_KEY = ""
DEFAULT_TIMEOUT = 10
DOMAIN = "deepstack_face"
PLATFORMS = ["image_processing"]

CLASSIFIER = "deepstack_face"
DATA_DEEPSTACK = "deepstack_classifiers"
FILE_PATH = "file_path"
SERVICE_TEACH_FACE = "teach_face"


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_IP_ADDRESS): cv.string,
        vol.Required(CONF_PORT): cv.port,
        vol.Optional(CONF_PREVIEW_FACES_FOLDER): cv.isdir, 
        vol.Optional(CONF_API_KEY, default=DEFAULT_API_KEY): cv.string,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        vol.Optional(CONF_DETECT_ONLY, default=False): cv.boolean,
        vol.Optional(CONF_SAVE_FILE_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_TIMESTAMPTED_FILE, default=False): cv.boolean,
        vol.Optional(CONF_SAVE_FACES_FOLDER): cv.isdir,
        vol.Optional(CONF_SAVE_FACES, default=False): cv.boolean,
        vol.Optional(CONF_SHOW_BOXES, default=True): cv.boolean,
    }
)

SERVICE_TEACH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(FILE_PATH): cv.string,
    }
)

DRAW_PREVIEW_FACE_SCHEMA = vol.Schema(
    {
        vol.Required(FILE_PATH): cv.string,
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
    }
)

def get_valid_filename(name: str) -> str:
    return re.sub(r"(?u)[^-\w.]", "", str(name).strip().replace(" ", "_"))


def get_faces(predictions: list, img_width: int, img_height: int):
    """Return faces with formatting for annotating images."""
    faces = []
    decimal_places = 3
    for pred in predictions:
        if not "userid" in pred.keys():
            name = "unknown"
        else:
            name = pred["userid"]
        confidence = round(pred["confidence"] * 100, decimal_places)
        box_width = pred["x_max"] - pred["x_min"]
        box_height = pred["y_max"] - pred["y_min"]
        box = {
            "height": round(box_height / img_height, decimal_places),
            "width": round(box_width / img_width, decimal_places),
            "y_min": round(pred["y_min"] / img_height, decimal_places),
            "x_min": round(pred["x_min"] / img_width, decimal_places),
            "y_max": round(pred["y_max"] / img_height, decimal_places),
            "x_max": round(pred["x_max"] / img_width, decimal_places),
        }
        faces.append(
            {"name": name, "confidence": confidence, "bounding_box": box, "prediction": pred}
        )
    return faces


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the classifier."""

    setup_reload_service(hass, DOMAIN, PLATFORMS)

    if DATA_DEEPSTACK not in hass.data:
        hass.data[DATA_DEEPSTACK] = []

    save_file_folder = config.get(CONF_SAVE_FILE_FOLDER)
    if save_file_folder:
        save_file_folder = Path(save_file_folder)

    save_faces_folder = config.get(CONF_SAVE_FACES_FOLDER)
    if save_faces_folder:
        save_faces_folder = Path(save_faces_folder)

    entities = []
    for camera in config[CONF_SOURCE]:
        face_entity = FaceClassifyEntity(
            config[CONF_IP_ADDRESS],
            config[CONF_PORT],
            config.get(CONF_API_KEY),
            config.get(CONF_TIMEOUT),
            config.get(CONF_DETECT_ONLY),
            save_file_folder,
            config.get(CONF_SAVE_TIMESTAMPTED_FILE),
            save_faces_folder,
            config.get(CONF_SAVE_FACES),
            config[CONF_SHOW_BOXES],
            camera[CONF_ENTITY_ID],
            config.get(CONF_PREVIEW_FACES_FOLDER),
        )
        entities.append(face_entity)
        hass.data[DATA_DEEPSTACK].append(face_entity)

    add_devices(entities)

    def service_handle(service):
        """Handle for services."""
        entity_ids = service.data.get("entity_id")

        classifiers = hass.data[DATA_DEEPSTACK]
        if entity_ids:
            classifiers = [c for c in classifiers if c.entity_id in entity_ids]

        for classifier in classifiers:
            name = service.data.get(ATTR_NAME)
            file_path = service.data.get(FILE_PATH)
            classifier.teach(name, file_path)
    
    def service_draw_boxes_on_preview(service):
        """Service to perform recognition of the number of faces in a picture and draw the boxes. 
        Also keeps internaly in the component the number of faces in the picture."""
        entity_id = service.data.get(ATTR_ENTITY_ID)
        classifier = None
        for putative_classifier in hass.data[DATA_DEEPSTACK]:
            if putative_classifier.entity_id == entity_id:
                classifier = putative_classifier
                break

        ##Make sure the entity ID exists
        if classifier == None:
            _LOGGER.error("Entity not found")
            return
                
        
        file_path = service.data.get(FILE_PATH)
        classifier.draw_boxes_on_preview(file_path)
                

    hass.services.register(
        DOMAIN, SERVICE_TEACH_FACE, service_handle, schema=SERVICE_TEACH_SCHEMA
    )

    hass.services.register(
        DOMAIN, "draw_boxes_on_preview", service_draw_boxes_on_preview, schema=DRAW_PREVIEW_FACE_SCHEMA,
    )


class FaceClassifyEntity(ImageProcessingFaceEntity):
    """Perform a face classification."""
    _attr_icon = "mdi:face-recognition"

    def __init__(
        self,
        ip_address,
        port,
        api_key,
        timeout,
        detect_only,
        save_file_folder,
        save_timestamped_file,
        save_faces_folder,
        save_faces,
        show_boxes,
        camera_entity,
        preview_folder,
        name=None,
    ):
        """Init with the API key and model id."""
        super().__init__()
        self._dsface = ds.DeepstackFace(
            ip=ip_address, port=port, api_key=api_key, timeout=timeout
        )
        self._detect_only = detect_only
        self._show_boxes = show_boxes
        self._last_detection = None
        self._save_file_folder = save_file_folder
        self._save_timestamped_file = save_timestamped_file
        self._save_faces_folder = save_faces_folder
        self._save_faces = save_faces
        self._n_faces_latest_preview = None #Keeps track of the number of faces in the lastest picture in the preview
        self._preview_image_folder = preview_folder
        self._preview_mode = False ##Add the preview folder
        self._camera = camera_entity
        if name:
            self._name = name
        else:
            camera_name = split_entity_id(camera_entity)[1]
            self._name = "{}_{}".format(CLASSIFIER, camera_name)
        self._predictions = []
        self._matched = {}
        self.total_faces = None

    def process_image(self, image):
        """Process an image, comes in as bytes."""
        self._predictions = []
        self._matched = {}
        self.total_faces = None

        try:
            pil_image = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        except UnidentifiedImageError:
            _LOGGER.warning("Deepstack unable to process image, bad data")
            return

        image_width, image_height = pil_image.size
        try:
            if self._detect_only:
                self._predictions = self._dsface.detect(image)
            else:
                self._predictions = self._dsface.recognize(image)
        except ds.DeepstackException as exc:
            _LOGGER.error("Depstack error : %s", exc)
            return

        if len(self._predictions) > 0:
            self._last_detection = dt_util.now().strftime(DATETIME_FORMAT)
            self.total_faces = len(self._predictions)
            self._matched = ds.get_recognized_faces(self._predictions)
            self.faces = get_faces(self._predictions, image_width, image_height)
            self.process_faces(
                self.faces, self.total_faces,
            )  # fire image_processing.detect_face

            if not self._detect_only:
                if self._save_faces and self._save_faces_folder:
                    self.save_faces(
                        pil_image, self._save_faces_folder
                    )

            if self._save_file_folder:
                self.save_image(
                    pil_image, self._save_file_folder,
                )
        
        if self._preview_mode: 
            directory = self._preview_image_folder
            timestamp_save_path = os.path.join(directory, self._preview_image_path)
            pil_image.save(timestamp_save_path)

    def draw_boxes_on_preview(self, file_path):
        """Open an image, and draw boxes on preview. Also keeps track of the number of faces in that picture."""
        self._preview_mode = True
        self._preview_image_path = file_path.split(".")[0] + "_preview.jpeg"
        self.total_faces = None
        try: 
            self.process_image(open(file_path, "rb").read())
        except ds.DeepstackException as exc:
            return
        
        total_faces = self.total_faces
        if total_faces == None: #If there are faces in the picture
            _LOGGER.info("No faces were detected in the picture")
        else: 
            self._n_faces_latest_preview = self.total_faces

        self._preview_image_path = None
        self._preview_mode = False
        self.total_faces = None


    def detect_faces(self, image) -> int:
        """Returns the faces in the picture being processed"""
        return self._dsface.detect(image)
        

    def teach(self, name: str, file_path: str):
        """Teach classifier a face name."""
        if not self.hass.config.is_allowed_path(file_path):
            return

        #Run recognition if this variable is set to none
        if self._n_faces_latest_preview == None:
            with open(file_path, "rb") as image:
                n_face = len(self.detect_faces(image))
        else: 
            n_face = self._n_faces_latest_preview
        

        #Note: The same image needs to be opened twice for the request to work
        with open(file_path, "rb") as image1:
            if n_face == 0: 
                _LOGGER.info("No face detected in %s", file_path)
            elif n_face > 1: 
                _LOGGER.info("Multiple faces detected in %s", file_path)
            else:
                self._dsface.register(name, image1)
                self._n_faces_latest_preview = None #Set this value to None if the face was taught correctly
                _LOGGER.info("Deepstack face taught name : %s", name)
                
        #Fire an event to notify the frontend and pyscript
        event_data = {
            "person_name": name, 
            "image": file_path, 
            "faces": n_face
        }
        self.hass.bus.async_fire(f"{DOMAIN}_teach_face", event_data)
            

    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera

    @property
    def name(self):
        """Return the name of the image processing."""
        return self._name

    @property
    def state(self):
        """Ensure consistent state."""
        return self.total_faces

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def force_update(self):
        """Force update to fire state events even if state has not changed."""
        return True

    @property
    def device_state_attributes(self):
        """Return the classifier attributes."""
        attr = {}
        if self._detect_only:
            attr[CONF_DETECT_ONLY] = self._detect_only
        if not self._detect_only:
            attr["total_matched_faces"] = len(self._matched)
            attr["matched_faces"] = self._matched
        if self._last_detection:
            attr["last_detection"] = self._last_detection
        if self._camera:
            attr["camera_entity"] = self._camera
        attr["domain"] = DOMAIN
        return attr


    def save_faces(self, pil_image: Image, directory: Path):
        """Saves recognized faces."""
        for face in self.faces:
            box = face["prediction"]
            name = face["name"]
            confidence = face["confidence"]
            face_name = face["name"]

            cropped_image = pil_image.crop(
                (box["x_min"], box["y_min"], box["x_max"], box["y_max"])
            )

            timestamp_save_path = directory / f"{face_name}_{confidence:.1f}_{self._last_detection}.jpg"
            cropped_image.save(timestamp_save_path)
            _LOGGER.info("Deepstack saved face %s", timestamp_save_path)

    def save_image(self, pil_image: Image, directory: Path):
        """Draws the actual bounding box of the detected objects."""
        image_width, image_height = pil_image.size
        draw = ImageDraw.Draw(pil_image)
        for face in self.faces:
            if not self._show_boxes:
                break
            name = face["name"]
            confidence = face["confidence"]
            box = face["bounding_box"]
            box_label = f"{name}: {confidence:.1f}%"

            draw_box(
                draw,
                (box["y_min"], box["x_min"], box["y_max"], box["x_max"]),
                image_width,
                image_height,
                text=box_label,
                color=RED,
            )

        #@zroger499. If running on preview mode do not save as latest image
        if self._preview_mode == False:
            latest_save_path = (
                directory / f"{get_valid_filename(self._name).lower()}_latest.jpg"
            )
            pil_image.save(latest_save_path)

        if self._save_timestamped_file:
            if self._preview_mode == True:
                directory = self._preview_image_folder
                timestamp_save_path = os.path.join(directory, self._preview_image_path)
            else: 
                timestamp_save_path = directory / f"{self._name}_{self._last_detection}.jpg"
            
            pil_image.save(timestamp_save_path)
            _LOGGER.info("Deepstack saved file %s", timestamp_save_path)

        