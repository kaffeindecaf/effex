
import sys
import subprocess
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python stretch_video.py "input.mp4"')
        print()
        print("Example:")
        print('  python stretch_video.py "C:\\Videos\\video.mp4"')
        return

    input_file = Path(sys.argv[1])

    if not input_file.is_file():
        print(f"ERROR: File not found:")
        print(input_file)
        return

    # ==============================
    # PHONE OUTPUT SIZE
    # ==============================
    WIDTH = 1080
    HEIGHT = 1920

    output_file = input_file.with_name(
        input_file.stem + "_STRETCHED.mp4"
    )

    # ==============================
    # FORCE THE VIDEO TO THESE PIXELS
    #
    # scale=1080:1920
    #   Forces the actual image dimensions.
    #
    # setsar=1
    #   Forces square pixels.
    #
    # setdar=9/16
    #   Forces the display aspect ratio to 9:16.
    # ==============================

    video_filter = (
        f"scale={WIDTH}:{HEIGHT}:"
        f"force_original_aspect_ratio=disable,"
        f"setsar=1,"
        f"setdar={WIDTH}/{HEIGHT}"
    )

    command = [
        "ffmpeg",

        "-i", str(input_file),

        # Force removal of rotation metadata
        "-metadata:s:v:0", "rotate=0",

        # ACTUAL stretching happens here
        "-vf", video_filter,

        # Video
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",

        # Audio
        "-c:a", "aac",
        "-b:a", "192k",

        # Make the MP4 stream compatible with phones
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",

        "-y",
        str(output_file)
    ]

    print()
    print("================================")
    print("       VIDEO STRETCHER")
    print("================================")
    print()
    print(f"Input : {input_file}")
    print(f"Output: {output_file}")
    print()
    print(f"FORCING: {WIDTH} x {HEIGHT}")
    print("ASPECT RATIO: 9:16")
    print("CROPPING: NO")
    print("LETTERBOX: NO")
    print("DISTORTION: YES")
    print()

    try:
        subprocess.run(command, check=True)

        print()
        print("================================")
        print("             DONE")
        print("================================")
        print()
        print(f"Output saved to:")
        print(output_file)

    except FileNotFoundError:
        print()
        print("ERROR: FFmpeg is not installed or")
        print("FFmpeg is not available in PATH.")
        print()
        print("Test with:")
        print("    ffmpeg -version")

    except subprocess.CalledProcessError:
        print()
        print("ERROR: FFmpeg failed.")


if __name__ == "__main__":
    main()
