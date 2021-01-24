import atexit
import datetime
import logging
import os
import subprocess
import tempfile
import time
from enum import Enum
from shutil import which

from meta import FileMeta
from metacache import MetaCache
from util import open_if_exists

log = logging.getLogger(__name__)


class ConcatStrategy(Enum):
    CONCAT_PROTOCOL = 0
    CONCAT_FILTER = 1
    CONCAT_DEMUX = 2


class Preset:
    def __init__(self, ffmpeg_params, complex_filters, description, concat_strategy):
        self.ffmpeg_params = ffmpeg_params
        self.description = description
        self.concat_strategy = concat_strategy
        self.complex_filters = complex_filters

    def build_ffmpeg_params(self, input_files):
        args = []
        if self.concat_strategy == ConcatStrategy.CONCAT_PROTOCOL:
            args += ["-i", "concat:{}".format('|'.join(input_files))]
        elif self.concat_strategy == ConcatStrategy.CONCAT_FILTER:
            args += [a for b in [["-i", f] for f in input_files] for a in b]
            args += [
                "-filter_complex",
                f"concat=n={len(input_files)}:v=1:a=1[catv][outa];[catv]" + ",".join(self.complex_filters) + "[outv]",
            ]
            args += ["-map", "[outv]", "-map", "[outa]"]
        elif self.concat_strategy == ConcatStrategy.CONCAT_DEMUX:
            tfh, tempfile_path = tempfile.mkstemp(text=True)
            atexit.register(lambda: os.unlink(tempfile_path))
            with os.fdopen(tfh, 'w') as tf:
                for input_file in input_files:
                    path = input_file.replace('\\', '/')
                    print(f"file 'file:{path}'", file=tf)

            args += ['-f', 'concat', '-safe', '0', '-i', tempfile_path]

        args += self.ffmpeg_params
        return args


encode_presets = {
    "copy": Preset(
        ["-c", "copy"],
        [],
        "Directly copy input to output. Uses the FFMPEG concat demuxer to concatenate without re-encoding. "
        "Only suited for concatenating files with the exact same codecs and parameters (e.g. scenes from a camera).",
        ConcatStrategy.CONCAT_DEMUX
    ),

    "copydv": Preset(
        ["-c", "copy"],
        [],
        "Directly copy input to output. Only suited for MPEG-2 (includes DV) files with equal codec properties due to "
        "use of the concatenation protocol.",
        ConcatStrategy.CONCAT_PROTOCOL
    ),

    "1080p": Preset(
        ["-c:v", "libx265", "-crf", "28", "-preset", "medium", "-c:a", "flac"],
        ["scale=-1:1080"],
        "Transcode to 1080p using libx265 with a CRF of 28 and FLAC audio. Suited for any input format.",
        ConcatStrategy.CONCAT_FILTER
    ),

    "4k": Preset(
        ["-c:v", "libx265", "-crf", "28", "-preset", "medium", "-c:a", "flac"],
        ["scale=-1:2160"],
        "Transcode to 4k UHD using libx265 with a CRF of 28 and FLAC audio. Suited for any input format.",
        ConcatStrategy.CONCAT_FILTER
    )
}


class FileList:
    def __init__(self, mediatools: 'MediaTools', metacache: 'MetaCache'):
        self.paths = []
        self.meta = {}
        self._mediatools = mediatools
        self._metacache = metacache

    def add_file(self, path):
        self.meta[path] = self._metacache.get(path, self._mediatools.get_meta)
        self.paths.append(path)

    def get_meta(self, path):
        return self.meta.get(path)

    def get_paths(self):
        return self.paths

    def sort_by_path(self):
        self.paths = sorted(self.paths)

    def sort_by_filename(self):
        self.paths = sorted(self.paths, key=os.path.basename)

    def sort_by_datetime(self):
        self.paths = sorted(self.paths, key=self._get_sort_datetime)

    def _get_sort_datetime(self, path):
        dt = self.meta[path].datetime
        if dt is None:
            log.warning("No recorded date for %s; inserting at beginning", path)
            return datetime.datetime.fromtimestamp(0)
        return dt


class MediaToolsNotInstalledException(Exception):
    pass


class MediaTools:
    def __init__(self):
        self.mediainfo_exe = which("mediainfo")
        if self.mediainfo_exe is None:
            raise MediaToolsNotInstalledException(
                "mediainfo commandline tool not found. Use e.g. sudo apt install mediainfo (on Debian/Ubuntu) "
                "or choco install mediainfo-cli (on Windows with Chocolatey) to install it."
            )

        self.ffmpeg_exe = which("ffmpeg") or which("avconv")
        if self.ffmpeg_exe is None:
            raise MediaToolsNotInstalledException(
                "ffmpeg or avconv commandline tool not found. Use e.g. sudo apt install ffmpeg (on Debian/Ubuntu) "
                "or choco install ffmpeg (on Windows with Chocolatey) to install it."
            )

    def get_meta(self, file):
        result = subprocess.run([self.mediainfo_exe, "--fullscan", file], stdout=subprocess.PIPE)
        output = result.stdout.decode('utf8')
        info = FileMeta()

        for line in output.splitlines():
            if line.startswith("Recorded date") and not info.datetime:
                info.datetime = datetime.datetime.strptime(line.split(": ", 1)[1], '%Y-%m-%d %H:%M:%S.000')
            if line.startswith("Tagged date") and not info.datetime:
                info.datetime = datetime.datetime.strptime(line.split(": ", 1)[1], '%Z %Y-%m-%d %H:%M:%S')
            if line.startswith("Duration") and not info.milliseconds:
                try:
                    info.milliseconds = int(line.split(": ", 1)[1])
                except ValueError:
                    pass
            if line.startswith("Frame count") and not info.frames:
                info.frames = int(line.split(": ", 1)[1])

        return info

    def do_concatenation(self, files, output, preset: Preset, logfile_path):
        with open_if_exists(logfile_path, "wb") as f:
            logfile_handle = f if f else subprocess.DEVNULL

            log.info("Starting video processing. This can take a while...")

            args = [self.ffmpeg_exe] + preset.build_ffmpeg_params(files) + ["-y", output]
            log.debug("Executing: %s", " ".join("'" + a + "'" for a in args))

            proc = subprocess.Popen(args, stdout=logfile_handle, stderr=logfile_handle, stdin=subprocess.DEVNULL)

            if logfile_path:
                with open(logfile_path, "r") as logfile_in:
                    test = None
                    prev_line = ""
                    while proc.poll() is None:
                        line = test
                        test = logfile_in.readline()
                        if not test:
                            if line:
                                if line.startswith("frame="):
                                    line_shortness = (len(prev_line) - len(line))
                                    padding = ((" " * line_shortness) if line_shortness > 0 else "")
                                    print(line.strip() + padding, end="\r", flush=True)
                                    prev_line = line
                            time.sleep(.5)

                    print()
                    if proc.returncode != 0:
                        log.error("Encoding failed. Check the log file for the error.")
            else:
                proc.wait()
                if proc.returncode != 0:
                    log.error("Encoding failed. Re-run with logging to find out what went wrong.")

            if proc.returncode == 0:
                log.info("Processing done.")
