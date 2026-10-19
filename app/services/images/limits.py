"""Product limits for general-purpose image uploads.

An upload is accepted up to `MAX_UPLOAD_BYTES`. The object written to the
bucket is at most `MAX_STORED_BYTES`; processing reduces it until it fits.
`MAX_DECODE_PIXELS` and `MAX_DECODE_EDGE` are memory guards. They stop a
compressed payload from expanding into a bitmap the process cannot decode.

`TEMPORARY_ACCOUNT_IMAGE_CAP` is a stand-in quota enforced by listing the
account prefix after each upload. Remove the constant and the checks in
`ImageService.create` when accounts may store unlimited images.
"""

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_BODY_OVERHEAD_BYTES = 64 * 1024
MAX_UPLOAD_BODY_BYTES = MAX_UPLOAD_BYTES + UPLOAD_BODY_OVERHEAD_BYTES
MAX_STORED_BYTES = 1024 * 1024
MAX_DECODE_EDGE = 65535
MAX_DECODE_PIXELS = 100_000_000
TEMPORARY_ACCOUNT_IMAGE_CAP = 10
MAX_LISTED_IMAGES = 1000
