from threading import Thread

from flask import Flask, Response, request
from pipert.core.component import BaseComponent
from queue import Empty
from multiprocessing import Process
import cv2
import time
import requests


# Not working for some reason, the flask_display queue is empty forever ????????
class FlaskVideoDisplay(BaseComponent):

    def __init__(self, component_config):
        self.display_queue_name = "flask_display"
        self.create_queue(self.display_queue_name)
        super().__init__(component_config)

        app = Flask(__name__)

        self.flask_app = app

        @app.route('/video')
        def video_feed():
            return Response(self._gen(),
                            mimetype='multipart/x-mixed-replace; '
                                     'boundary=frame')

        def shutdown_server():
            func = request.environ.get('werkzeug.server.shutdown')
            if func is None:
                raise RuntimeError('Not running with the Werkzeug Server')
            func()

        @app.route('/shutdown')
        def shutdown():
            # app.do_teardown_appcontext()
            shutdown_server()
            return 'Server shutting down...'

    def _start(self):
        super()._start()
        # TODO need to understand why working as thread and not as process
        self.server = Thread(target=self.flask_app.run, kwargs={"host": '0.0.0.0'})
        self.server.start()

    def stop_run(self):
        try:
            super().stop_run()
            self.server.join()
            return 0
        except RuntimeError:
            return 1

    def _gen(self):
        q = self.get_queue("flask_display")
        while not self.stop_event.is_set():
            try:
                msg = q.get(block=False)
                image = msg.get_payload()
                ret, frame = cv2.imencode('.jpg', image)
                frame = frame.tobytes()
                yield (b'--frame\r\n'
                       b'Pragma-directive: no-cache\r\n'
                       b'Cache-directive: no-cache\r\n'
                       b'Cache-control: no-cache\r\n'
                       b'Pragma: no-cache\r\n'
                       b'Expires: 0\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')
            except Empty:
                time.sleep(0)

    def _teardown_callback(self, *args, **kwargs):
        # self.server.terminate()
        _ = requests.get("http://127.0.0.1:5000/shutdown")
        # self.server.terminate()
        # print("kill!!!")
        # self.server.kill()

    def get_all_queue_names(self):
        queue_names = list(self.queues.keys())
        queue_names.remove(self.display_queue_name)
        return queue_names
