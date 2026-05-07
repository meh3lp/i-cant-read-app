import typing
import threading

if typing.TYPE_CHECKING:
    import redis as _redis
    from management.dispatcher import Dispatcher
    from management.i_cant_read import ICantRead
    from management.dispatcher import Dispatcher

class Listener:
    '''
    Base class for all listeners
    '''
    app: "ICantRead"
    dispatcher: "Dispatcher"
    stop_event: "threading.Event"
    redis: "_redis.Redis"

    def __init__(
        self,
        app: "ICantRead",
        dispatcher: "Dispatcher",
        stop_event: "threading.Event",
        redis: "_redis.Redis",
    ):
        self.app = app
        self.dispatcher = dispatcher
        self.stop_event = stop_event
        self.redis = redis

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError
