"""One frame from a webcam, printed as a FRAME line for home/presence.py, which runs this in a
process of its own: some camera drivers (NVIDIA Broadcast's, for one) write their logs straight to
the console, which would scribble over Cora's screen, and OpenCV stays out of her own memory. The
frame goes back over the pipe and is never written anywhere.

    python -I camera_grab.py <camera index>
"""

import base64
import sys

# Long enough for the camera to settle its exposure; the first frames come out dark.
WARM_UP_FRAMES = 12
WIDTH = 640
JPEG_QUALITY = 70


def main() -> int:
    import cv2

    index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    camera = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    try:
        if not camera.isOpened():
            print("ERROR no camera", flush=True)
            return 2
        frame = None
        for _ in range(WARM_UP_FRAMES):
            ok, read = camera.read()
            if ok:
                frame = read
    finally:
        camera.release()
    if frame is None:
        print("ERROR no frame", flush=True)
        return 3
    height, width = frame.shape[:2]
    if width > WIDTH:
        frame = cv2.resize(frame, (WIDTH, round(height * WIDTH / width)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        print("ERROR not encoded", flush=True)
        return 4
    print("FRAME " + base64.b64encode(jpeg.tobytes()).decode("ascii"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
