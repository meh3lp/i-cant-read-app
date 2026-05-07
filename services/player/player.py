import typing
import threading

if typing.TYPE_CHECKING:
    import redis as _redis
    from management.i_cant_read import ICantRead

class Player:
    '''
    Base class for all Players
    '''
    app: "ICantRead"
    redis: "_redis.Redis"
    stop_event: "threading.Event"

    def __init__(
        self,
        app: "ICantRead",
        redis: "_redis.Redis",
        stop_event: "threading.Event",
    ):
        self.app = app
        self.redis = redis
        self.stop_event = stop_event

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError
