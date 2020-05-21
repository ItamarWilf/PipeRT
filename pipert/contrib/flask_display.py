import argparse
from typing import Optional
from urllib.parse import urlparse
from flask import Flask, Response

from pipert.contrib.metrics_collectors.prometheus_collector import PrometheusCollector
from pipert.core.component import BaseComponent
from pipert.contrib.metrics_collectors.splunk_collector import SplunkCollector
from pipert.core.metrics_collector import NullCollector
from pipert.core.routine import Routine
import queue
from threading import Thread
import cv2
from pipert.utils.visualizer import VideoVisualizer
from pipert.utils.visualizer.catalog import MetadataCatalog
from pipert.core.message import message_decode, Message
from pipert.core.message_handlers import RedisHandler
from pipert.core import QueueHandler
import time
import os


def gen(q: QueueHandler):
    while True:
        encoded_frame = q.non_blocking_get()
        if encoded_frame:
            yield (b'--frame\r\n'
                   b'Pragma-directive: no-cache\r\n'
                   b'Cache-directive: no-cache\r\n'
                   b'Cache-control: no-cache\r\n'
                   b'Pragma: no-cache\r\n'
                   b'Expires: 0\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + encoded_frame + b'\r\n\r\n')


class MetaAndFrameFromRedis(Routine):

    def __init__(self, in_key_meta, in_key_im, url, queue, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.in_key_meta = in_key_meta
        self.in_key_im = in_key_im
        self.url = url
        self.q_handler = QueueHandler(queue)
        self.msg_handler = None
        self.flip = False
        self.negative = False

    def receive_msg(self, in_key, most_recent=True):
        if most_recent:
            encoded_msg = self.msg_handler.read_most_recent_msg(in_key)
        else:
            encoded_msg = self.msg_handler.receive(in_key)
        if not encoded_msg:
            return None
        msg = message_decode(encoded_msg)
        msg.record_entry(self.component_name, self.logger)
        return msg

    def main_logic(self, *args, **kwargs):
        pred_msg = self.receive_msg(self.in_key_meta, most_recent=True)
        frame_msg = self.receive_msg(self.in_key_im, most_recent=False)
        if frame_msg:
            arr = frame_msg.get_payload()

            if self.flip:
                arr = cv2.flip(arr, 1)

            if self.negative:
                arr = 255 - arr

            frame_msg.update_payload(arr)
            success = self.q_handler.deque_non_blocking_put((frame_msg, pred_msg))
            return success
        else:
            time.sleep(0)
            return False

    def setup(self, *args, **kwargs):
        self.msg_handler = RedisHandler(self.url)

    def cleanup(self, *args, **kwargs):
        self.msg_handler.close()


class VisLogic(Routine):
    def __init__(self, in_queue, out_queue, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.in_queue = QueueHandler(in_queue)
        self.out_queue = QueueHandler(out_queue)
        self.vis = VideoVisualizer(MetadataCatalog.get("coco_2017_train"))
        self.latest_pred_msg = None

    def main_logic(self, *args, **kwargs):
        # TODO implement input that takes both frame and metadata
        messages = self.in_queue.non_blocking_get()
        if messages:
            frame_msg, pred_msg = messages
            if pred_msg is not None:
                self.latest_pred_msg = pred_msg
            pred_msg = self.latest_pred_msg
            self.draw_preds_on_frame(frame_msg, pred_msg)
            self.pass_frame_to_flask(frame_msg, pred_msg)
            return True
        else:
            return None

    def draw_preds_on_frame(self, frame_msg, pred_msg: Optional[Message]):
        if pred_msg is not None and not pred_msg.is_empty():
            frame = frame_msg.get_payload()
            pred = pred_msg.get_payload()
            image = self.vis.draw_instance_predictions(frame, pred, args_dict['names']) \
                .get_image()
            frame_msg.update_payload(image)

    def pass_frame_to_flask(self, frame_msg, pred_msg: Optional[Message]):
        image = frame_msg.get_payload()
        _, frame = cv2.imencode('.jpg', image)
        frame = frame.tobytes()
        frame_msg.record_exit(self.component_name, self.logger)
        if pred_msg is not None and not pred_msg.reached_exit:
            pred_msg.record_exit(self.component_name, self.logger)
            latency = pred_msg.get_end_to_end_latency(self.component_name)
            if latency is not None:
                self.metrics_collector.collect_latency(latency, self.component_name)
        success = self.out_queue.deque_non_blocking_put(frame)
        return success

    def setup(self, *args, **kwargs):
        self.state.dropped = 0

    def cleanup(self, *args, **kwargs):
        pass


class FlaskVideoDisplay(BaseComponent):

    def __init__(self, in_key_meta, in_key_im, redis_url, metrics_collector, endpoint,
                 name="FlaskVideoDisplay"):
        super().__init__(endpoint, name, metrics_collector)
        self.queue = queue.Queue(maxsize=1)
        self.t_get = MetaAndFrameFromRedis(in_key_meta, in_key_im, redis_url,
                                           self.queue,
                                           name="get_frames_and_preds",
                                           component_name=self.name,
                                           metrics_collector=self.metrics_collector)
        self.t_get.as_thread()
        self.register_routine(self.t_get)

        self.queue2 = queue.Queue(maxsize=1)
        self.t_vis = VisLogic(self.queue, self.queue2, name="vis_logic",
                              component_name=self.name,
                              metrics_collector=self.metrics_collector).as_thread()
        self.register_routine(self.t_vis)

        app = Flask(__name__)
        app.debug = False

        @app.route('/video')
        def video_feed():
            return Response(gen(QueueHandler(self.queue2)),
                            mimetype='multipart/x-mixed-replace; '
                                     'boundary=frame')

        self.server = Thread(target=app.run, kwargs={"host": '0.0.0.0'})
        self.register_routine(self.server)

    def flip_im(self):
        self.t_get.flip = not self.t_get.flip

    def negative(self):
        self.t_get.negative = not self.t_get.negative


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input_im', help='Input stream key name', type=str, default='camera:0')
    parser.add_argument('-m', '--input_meta', help='Input stream key name', type=str, default='camera:2')
    parser.add_argument('--names', type=str, default='pipert/contrib/YoloResources/coco.names',
                        help='coco.names file path')
    parser.add_argument('-u', '--url', help='Redis URL', type=str, default='redis://127.0.0.1:6379')
    parser.add_argument('-z', '--zpc', help='zpc port', type=str, default='4246')
    parser.add_argument('--monitoring', help='Name of the monitoring service', type=str, default='prometheus')

    args = parser.parse_args()
    args_dict = vars(args)
    # Set up Redis connection
    # url = urlparse(args.url)
    url = os.environ.get('REDIS_URL')
    url = urlparse(url) if url is not None else urlparse(args.url)

    if args.monitoring == 'prometheus':
        collector = PrometheusCollector(8082)
    elif args.monitoring == 'splunk':
        collector = SplunkCollector()
    else:
        collector = NullCollector()

    zpc = FlaskVideoDisplay(args.input_meta, args.input_im, url, collector, f"tcp://0.0.0.0:{args.zpc}")
    print(f"run {zpc.name}")
    zpc.run()
    print(f"Killed {zpc.name}")
