import typing
if typing.TYPE_CHECKING:
    from management.i_cant_read import ICantRead


class RVC:
    '''
    Base class for all RVC
    '''
    app: "ICantRead"
    task: typing.Any

    def __init__(self, app: "ICantRead"):
        self.app = app
