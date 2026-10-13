import re


class BaselineExportError(RuntimeError):
    def __init__(self, code: str) -> None:
        if type(code) is not str or re.fullmatch("[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid baseline error code")
        self.code = code
        super().__init__(code)
