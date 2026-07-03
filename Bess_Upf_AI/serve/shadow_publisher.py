# ponytail: stub for Task 5; replace with real Kafka publisher then
class ShadowPublisher:
    def __init__(self, producer=None, topic="upf.shadow.detections"):
        self._producer = producer
        self._topic = topic

    def publish(self, upf_id, ts, result):
        pass
