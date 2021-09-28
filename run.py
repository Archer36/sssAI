#!/usr/bin/env python3

import os
import logging
import sys
import pathlib
from json import loads

from gunicorn.app.base import BaseApplication
from gunicorn.glogging import Logger
from loguru import logger

from app.main import app


def load_settings(file_path: pathlib.Path = None) -> dict:
    if file_path is None:
        logging.fatal("A path to the settings file must be specified")
    else:
        try:
            unparsed_settings_file = file_path.read_text()
        except FileNotFoundError:
            logging.fatal(f"Can't find the settings JSON file at the path {file_path}")
            raise()
        else:
            settings = loads(unparsed_settings_file)
            return settings


class InterceptHandler(logging.Handler):
    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


class StubbedGunicornLogger(Logger):
    def setup(self, cfg):
        handler = logging.NullHandler()
        self.error_logger = logging.getLogger("gunicorn.error")
        self.error_logger.addHandler(handler)
        self.error_log.setLevel(LOG_LEVEL)
        self.access_log.setLevel(LOG_LEVEL)


class StandaloneApplication(BaseApplication):
    """Our Gunicorn application."""

    def __init__(self, app, options=None):
        self.options = options or {}
        self.application = app
        super().__init__()

    def load_config(self):
        config = {
            key: value for key, value in self.options.items()
            if key in self.cfg.settings and value is not None
        }
        for key, value in config.items():
            self.cfg.set(key.lower(), value)

    def load(self):
        return self.application


if __name__ == '__main__':
    settings_file_path = pathlib.Path.cwd().joinpath("config", "settings.json")
    settings = load_settings(settings_file_path)

    settings_log_level = settings.get("log_level")
    settings_access_log = settings.get("access_log")
    settings_error_log = settings.get("error_log")
    settings_sssai_api_port = settings.get("sssai_api_port")
    settings_json_logs = settings.get("json_logs")
    settings_gunicorn_workers = settings.get("gunicorn_workers")

    if settings_log_level:
        LOG_LEVEL = logging.getLevelName(settings_log_level)
    else:
        LOG_LEVEL = logging.getLevelName("INFO")

    if settings_sssai_api_port:
        SSSAI_API_PORT = settings_sssai_api_port
    else:
        SSSAI_API_PORT = 4242

    if settings_json_logs:
        JSON_LOGS = settings_json_logs
    else:
        JSON_LOGS = False

    if settings_gunicorn_workers:
        WORKERS = settings_gunicorn_workers
    else:
        WORKERS = 5

    intercept_handler = InterceptHandler()
    # logging.basicConfig(handlers=[intercept_handler], level=LOG_LEVEL)
    # logging.root.handlers = [intercept_handler]
    logging.root.setLevel(LOG_LEVEL)

    seen = set()

    logger.configure(handlers=[{"sink": sys.stdout, "serialize": JSON_LOGS}])

    options = {
        "bind": f"0.0.0.0:{SSSAI_API_PORT}",
        "workers": WORKERS,
        "accesslog": settings_access_log,
        "errorlog": settings_error_log,
        "worker_class": "uvicorn.workers.UvicornWorker"
        #"logger_class": StubbedGunicornLogger
    }

    StandaloneApplication(app, options).run()
