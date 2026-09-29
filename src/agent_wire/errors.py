# SPDX-License-Identifier: AGPL-3.0-only
class WireError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Offline(WireError):
    def __init__(self, message: str):
        super().__init__("offline", message)


class DeliveryUnknown(WireError):
    def __init__(self, message: str):
        super().__init__("delivery_unknown", message)
