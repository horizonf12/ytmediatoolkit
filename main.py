import sys
import os
import time
import shutil
import tempfile
import subprocess
import threading
import math

import yt_dlp

from PySide6.QtCore import (
    QThread,
    QAbstractAnimation,
    Signal,
    QUrl,
    Qt,
    QRect,
    QPropertyAnimation,
    QEvent,
    QEasingCurve,
    QParallelAnimationGroup,
    QSequentialAnimationGroup,
    QTimer,
    QSize,
    QPoint,
    Property,
    QRectF,
)
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QPainter,
    QPen,
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QLinearGradient,
    QRadialGradient,
    QPainterPath,
    QCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QProgressBar,
    QComboBox,
    QListView,
    QAbstractItemView,
    QStyledItemDelegate,
    QStyle,
    QTabWidget,
    QGroupBox,
    QFormLayout,
    QDialog,
    QTextEdit,
    QSizePolicy,
    QFrame,
    QStackedWidget,
    QGridLayout,
    QScrollArea,
    QGraphicsDropShadowEffect,
)


# ============================================================
# HELPERS
# ============================================================

def format_bytes(value):
    if value is None:
        return "—"

    value = float(value)

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024

    return "—"


def format_duration(seconds):
    if seconds is None:
        return "—"

    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "—"

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    return f"{minutes:02d}:{seconds:02d}"


def bundled_binary_path(name):
    """Return the FFmpeg/ffprobe binary from the app bundle or PATH."""
    if getattr(sys, "frozen", False):
        candidates = []

        # PyInstaller may place bundled binaries under the runtime
        # directory or next to the frozen executable depending on the
        # platform/build layout. Check both, plus the macOS Frameworks
        # location used by recent PyInstaller releases.
        meipass = getattr(sys, "_MEIPASS", None)
        executable_dir = os.path.dirname(os.path.abspath(sys.executable))

        if meipass:
            candidates.append(os.path.join(meipass, name))

        candidates.extend([
            os.path.join(executable_dir, name),
            os.path.abspath(os.path.join(executable_dir, "..", "Frameworks", name)),
            os.path.abspath(os.path.join(executable_dir, "..", "Resources", name)),
        ])

        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    return shutil.which(name) or name


def ffmpeg_location():
    """Return a directory yt-dlp can use to find ffmpeg and ffprobe."""
    path = ffmpeg_path()
    if os.path.isfile(path):
        return os.path.dirname(os.path.abspath(path))
    return path


def ffmpeg_path():
    return bundled_binary_path("ffmpeg")


def ffprobe_path():
    return bundled_binary_path("ffprobe")


def ffmpeg_exists():
    path = ffmpeg_path()
    return os.path.isfile(path) or shutil.which(path) is not None


def ffprobe_exists():
    path = ffprobe_path()
    return os.path.isfile(path) or shutil.which(path) is not None


# ============================================================
# DROP-AWARE LINE EDIT
# ============================================================

class FileDropLineEdit(QLineEdit):
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    event.acceptProposedAction()
                    return

        event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                path = url.toLocalFile()
                self.setText(path)
                self.file_dropped.emit(path)
                event.acceptProposedAction()
                return

        event.ignore()


# ============================================================
# DOWNLOAD WORKER
# ============================================================

class DownloadWorker(QThread):

    progress = Signal(int)
    speed = Signal(str)
    eta = Signal(str)
    status = Signal(str)

    finished = Signal()
    cancelled = Signal()
    error = Signal(str)

    def __init__(
        self,
        url,
        output_folder,
        format_choice,
        quality_choice,
        codec_choice,
        audio_quality,
    ):
        super().__init__()

        self.url = url
        self.output_folder = output_folder

        self.format_choice = format_choice
        self.quality_choice = quality_choice
        self.codec_choice = codec_choice
        self.audio_quality = audio_quality

        self.cancel_requested = False
        self.ffmpeg_process = None

        self.ffmpeg_stderr_lines = []
        self.current_phase = None

    def cancel(self):
        self.cancel_requested = True
        self.status.emit("Stopping...")

        if self.ffmpeg_process is not None:
            if self.ffmpeg_process.poll() is None:
                try:
                    self.ffmpeg_process.terminate()
                except Exception:
                    pass

    def is_audio_format(self):
        return self.format_choice in [
            "MP3",
            "M4A",
            "WAV",
            "FLAC",
            "Opus",
        ]

    def get_video_format(self):
        if self.quality_choice == "Best Available":
            height_filter = ""
        elif self.quality_choice == "1080p":
            height_filter = "[height<=1080]"
        elif self.quality_choice == "720p":
            height_filter = "[height<=720]"
        elif self.quality_choice == "480p":
            height_filter = "[height<=480]"
        elif self.quality_choice == "360p":
            height_filter = "[height<=360]"
        else:
            height_filter = ""

        if self.codec_choice == "H.264":
            video = f"bv*[vcodec^=avc1]{height_filter}"
        elif self.codec_choice == "VP9":
            video = f"bv*[vcodec^=vp9]{height_filter}"
        elif self.codec_choice == "AV1":
            video = f"bv*[vcodec^=av01]{height_filter}"
        else:
            video = f"bv*{height_filter}"

        return f"{video}+ba/b"

    def progress_hook(self, data):
        if self.cancel_requested:
            raise yt_dlp.utils.DownloadCancelled(
                "Cancelled by user."
            )

        info = data.get("info_dict", {}) or {}

        vcodec = info.get("vcodec")
        acodec = info.get("acodec")

        if vcodec and vcodec != "none":
            phase = "video"
        elif acodec and acodec != "none":
            phase = "audio"
        else:
            phase = self.current_phase or "video"

        if phase != self.current_phase:
            self.current_phase = phase

            if phase == "video":
                self.status.emit("Downloading video stream...")
            else:
                self.status.emit("Downloading audio stream...")

        if data["status"] == "downloading":
            downloaded = data.get(
                "downloaded_bytes",
                0
            )

            total = (
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
            )

            if total:
                percentage = int(
                    downloaded / total * 100
                )

                if self.is_audio_format():
                    overall = percentage
                elif self.current_phase == "audio":
                    overall = 70 + int(percentage * 0.3)
                else:
                    overall = int(percentage * 0.7)

                self.progress.emit(overall)

            download_speed = data.get("speed")

            if download_speed:
                mb_speed = (
                    download_speed
                    / 1024
                    / 1024
                )

                self.speed.emit(
                    f"{mb_speed:.2f} MB/s"
                )

            eta_seconds = data.get("eta")

            if eta_seconds is not None:
                self.eta.emit(
                    format_duration(eta_seconds)
                )

        elif data["status"] == "finished":
            if self.current_phase == "video" and not self.is_audio_format():
                self.status.emit("Video stream downloaded.")
            elif self.current_phase == "audio":
                self.status.emit("Audio stream downloaded.")

    def download_video(self):
        output_template = os.path.join(
            self.output_folder,
            "%(title)s.%(ext)s"
        )

        options = {
            "format": self.get_video_format(),
            "outtmpl": output_template,
            "progress_hooks": [self.progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "merge_output_format": self.format_choice.lower(),
            "ffmpeg_location": ffmpeg_path(),
            "windowsfilenames": True,
            "noplaylist": True,
        }

        self.status.emit("Downloading video...")

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([self.url])

    def download_audio_source(self, temporary_folder):
        output_template = os.path.join(
            temporary_folder,
            "%(title)s.%(ext)s"
        )

        options = {
            "format": "ba/b",
            "outtmpl": output_template,
            "progress_hooks": [self.progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ffmpeg_location": ffmpeg_path(),
            "windowsfilenames": True,
            "noplaylist": True,
        }

        self.status.emit("Downloading audio...")

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                self.url,
                download=True
            )

            if info.get("acodec") in (None, "none"):
                raise Exception(
                    "The selected YouTube source does not contain an audio stream."
                )

            filename = ydl.prepare_filename(info)
            return filename

    def build_ffmpeg_command(self, input_file, output_file):
        if self.format_choice == "MP3":
            bitrate = self.audio_quality.replace(" kbps", "")

            return [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{bitrate}k",
                output_file,
            ]

        if self.format_choice == "M4A":
            bitrate = self.audio_quality.replace(" kbps", "")

            return [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-c:a",
                "aac",
                "-b:a",
                f"{bitrate}k",
                output_file,
            ]

        if self.format_choice == "WAV":
            return [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-c:a",
                "pcm_s16le",
                output_file,
            ]

        if self.format_choice == "FLAC":
            return [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-c:a",
                "flac",
                output_file,
            ]

        if self.format_choice == "Opus":
            bitrate = self.audio_quality.replace(" kbps", "")

            return [
                ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                input_file,
                "-vn",
                "-c:a",
                "libopus",
                "-b:a",
                f"{bitrate}k",
                output_file,
            ]

        return []

    def _drain_ffmpeg_stderr(self):
        try:
            for line in self.ffmpeg_process.stderr:
                self.ffmpeg_stderr_lines.append(line)

                if len(self.ffmpeg_stderr_lines) > 200:
                    del self.ffmpeg_stderr_lines[0]

        except Exception:
            pass

    def convert_audio(self, input_file, output_file):
        command = self.build_ffmpeg_command(
            input_file,
            output_file
        )

        if not command:
            raise Exception("Invalid audio format.")

        self.status.emit(
            "Converting with FFmpeg..."
        )

        self.ffmpeg_stderr_lines = []

        self.ffmpeg_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )

        stderr_thread = threading.Thread(
            target=self._drain_ffmpeg_stderr,
            daemon=True
        )

        stderr_thread.start()

        while True:
            if self.cancel_requested:
                if self.ffmpeg_process.poll() is None:
                    try:
                        self.ffmpeg_process.terminate()
                    except Exception:
                        pass

                try:
                    self.ffmpeg_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        self.ffmpeg_process.kill()
                    except Exception:
                        pass

                stderr_thread.join(timeout=2)
                return False

            return_code = self.ffmpeg_process.poll()

            if return_code is not None:
                break

            self.msleep(100)

        stderr_thread.join(timeout=2)

        if return_code != 0:
            error_output = "".join(
                self.ffmpeg_stderr_lines
            ).strip()

            raise Exception(
                error_output
                or "FFmpeg conversion failed."
            )

        return True

    def download_audio(self):
        temporary_folder = tempfile.mkdtemp(
            prefix="yt_downloader_"
        )

        try:
            source_file = self.download_audio_source(
                temporary_folder
            )

            if self.cancel_requested:
                return

            filename = os.path.basename(source_file)
            title = os.path.splitext(filename)[0]
            extension = self.format_choice.lower()

            output_file = os.path.join(
                self.output_folder,
                f"{title}.{extension}"
            )

            success = self.convert_audio(
                source_file,
                output_file
            )

            if not success:
                return

            if os.path.exists(output_file):
                self.progress.emit(100)
                self.speed.emit("Done")
                self.eta.emit("00:00")
                self.status.emit("Download complete!")

        finally:
            shutil.rmtree(
                temporary_folder,
                ignore_errors=True
            )

    def run(self):
        try:
            if not ffmpeg_exists():
                raise Exception(
                    "FFmpeg was not found. Install FFmpeg and try again."
                )

            os.makedirs(
                self.output_folder,
                exist_ok=True
            )

            if self.is_audio_format():
                self.download_audio()
            else:
                self.download_video()

            if self.cancel_requested:
                self.cancelled.emit()
                return

            self.progress.emit(100)
            self.finished.emit()

        except yt_dlp.utils.DownloadCancelled:
            self.cancelled.emit()

        except Exception as e:
            if self.cancel_requested:
                self.cancelled.emit()
            else:
                self.error.emit(str(e))

        finally:
            self.ffmpeg_process = None


# ============================================================
# CONVERTER WORKER
# ============================================================

class ConverterWorker(QThread):

    progress = Signal(int)
    status = Signal(str)
    speed = Signal(str)
    eta = Signal(str)

    finished = Signal(str)
    cancelled = Signal()
    error = Signal(str)

    def __init__(
        self,
        input_file,
        output_file,
        output_format,
        codec,
        quality,
        resolution,
        audio_quality,
    ):
        super().__init__()

        self.input_file = input_file
        self.output_file = output_file

        self.output_format = output_format
        self.codec = codec
        self.quality = quality
        self.resolution = resolution
        self.audio_quality = audio_quality

        self.cancel_requested = False
        self.ffmpeg_process = None

        self.duration = 0.0
        self.stderr_lines = []

    def cancel(self):
        self.cancel_requested = True
        self.status.emit("Stopping...")

        if self.ffmpeg_process is not None:
            if self.ffmpeg_process.poll() is None:
                try:
                    self.ffmpeg_process.terminate()
                except Exception:
                    pass

    def probe_duration(self):
        if not ffprobe_exists():
            return 0.0

        try:
            result = subprocess.run(
                [
                    ffprobe_path(),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    self.input_file,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )

            if result.returncode != 0:
                return 0.0

            return float(result.stdout.strip())

        except Exception:
            return 0.0

    def has_audio_stream(self):
        if not ffprobe_exists():
            return None

        try:
            result = subprocess.run(
                [
                    ffprobe_path(),
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    self.input_file,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )

            return bool(result.stdout.strip())

        except Exception:
            return False

    def get_crf(self):
        crf_map = {
            "High": 20,
            "Balanced": 24,
            "Smaller": 28,
        }

        if self.quality == "Source":
            return 18

        return crf_map.get(
            self.quality,
            24
        )

    def get_video_encoder(self):
        encoders = {
            "H.264": "libx264",
            "H.265": "libx265",
            "VP9": "libvpx-vp9",
            "AV1": "libsvtav1",
        }

        return encoders.get(
            self.codec
        )

    def get_scale_filter(self):
        scale_map = {
            "1080p": "scale=-2:1080:force_original_aspect_ratio=decrease",
            "720p": "scale=-2:720:force_original_aspect_ratio=decrease",
            "480p": "scale=-2:480:force_original_aspect_ratio=decrease",
            "360p": "scale=-2:360:force_original_aspect_ratio=decrease",
        }

        return scale_map.get(
            self.resolution
        )

    def build_command(self):
        command = [
            ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            self.input_file,
        ]

        is_audio = self.output_format in [
            "MP3",
            "M4A",
            "WAV",
            "FLAC",
            "Opus",
        ]

        if is_audio:
            command.extend([
                "-vn",
            ])

            if self.output_format == "MP3":
                bitrate = self.audio_quality.replace(
                    " kbps",
                    ""
                )

                command.extend([
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    f"{bitrate}k",
                ])

            elif self.output_format == "M4A":
                bitrate = self.audio_quality.replace(
                    " kbps",
                    ""
                )

                command.extend([
                    "-c:a",
                    "aac",
                    "-b:a",
                    f"{bitrate}k",
                ])

            elif self.output_format == "WAV":
                command.extend([
                    "-c:a",
                    "pcm_s16le",
                ])

            elif self.output_format == "FLAC":
                command.extend([
                    "-c:a",
                    "flac",
                ])

            elif self.output_format == "Opus":
                bitrate = self.audio_quality.replace(
                    " kbps",
                    ""
                )

                command.extend([
                    "-c:a",
                    "libopus",
                    "-b:a",
                    f"{bitrate}k",
                ])

        else:
            if self.codec == "Copy":
                command.extend([
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                ])

            else:
                encoder = self.get_video_encoder()

                if not encoder:
                    raise Exception(
                        "Invalid video codec."
                    )

                command.extend([
                    "-c:v",
                    encoder,
                ])

                crf = self.get_crf()

                if self.codec == "H.264":
                    command.extend([
                        "-preset",
                        "medium",
                        "-crf",
                        str(crf),
                    ])

                elif self.codec == "H.265":
                    command.extend([
                        "-preset",
                        "medium",
                        "-crf",
                        str(crf),
                    ])

                elif self.codec == "VP9":
                    command.extend([
                        "-crf",
                        str(crf),
                        "-b:v",
                        "0",
                    ])

                elif self.codec == "AV1":
                    command.extend([
                        "-crf",
                        str(crf),
                        "-preset",
                        "6",
                    ])

                if self.resolution != "Keep":
                    scale_filter = self.get_scale_filter()

                    if scale_filter:
                        command.extend([
                            "-vf",
                            scale_filter,
                        ])

                if self.output_format == "WebM":
                    command.extend([
                        "-c:a",
                        "libopus",
                        "-b:a",
                        "160k",
                    ])

                else:
                    command.extend([
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                    ])

        command.extend([
            "-progress",
            "pipe:1",
            "-nostats",
            self.output_file,
        ])

        return command

    def _drain_stderr(self):
        try:
            for line in self.ffmpeg_process.stderr:
                self.stderr_lines.append(line)

                if len(self.stderr_lines) > 250:
                    del self.stderr_lines[0]

        except Exception:
            pass

    def run(self):
        try:
            if not ffmpeg_exists():
                raise Exception(
                    "FFmpeg was not found. Install FFmpeg and try again."
                )

            if not os.path.isfile(self.input_file):
                raise Exception(
                    "Input file does not exist."
                )

            audio_outputs = {
                "MP3",
                "M4A",
                "WAV",
                "FLAC",
                "Opus",
            }

            has_audio = self.has_audio_stream()
            if self.output_format in audio_outputs and has_audio is False:
                raise Exception(
                    "This source file does not contain an audio stream.\n\n"
                    "Choose a video output format, or select a source file that contains audio."
                )

            self.duration = self.probe_duration()

            command = self.build_command()

            self.progress.emit(0)
            self.status.emit("Starting conversion...")

            self.stderr_lines = []

            self.ffmpeg_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            stderr_thread = threading.Thread(
                target=self._drain_stderr,
                daemon=True,
            )
            stderr_thread.start()

            last_elapsed = 0.0
            start_time = time.time()

            for raw_line in self.ffmpeg_process.stdout:
                if self.cancel_requested:
                    break

                line = raw_line.strip()

                if not line or "=" not in line:
                    continue

                key, value = line.split(
                    "=",
                    1
                )

                if key == "out_time_ms":
                    try:
                        elapsed = float(value) / 1_000_000
                    except ValueError:
                        continue

                    last_elapsed = elapsed

                    if self.duration > 0:
                        percent = int(
                            min(
                                100,
                                max(
                                    0,
                                    elapsed
                                    / self.duration
                                    * 100,
                                ),
                            )
                        )

                        self.progress.emit(
                            percent
                        )

                        self.status.emit(
                            f"Converting... {percent}%"
                        )

                        remaining = max(
                            0,
                            self.duration - elapsed,
                        )

                        self.eta.emit(
                            format_duration(
                                remaining
                            )
                        )

                    elapsed_wall = max(
                        0.001,
                        time.time() - start_time,
                    )

                    effective_speed = (
                        elapsed
                        / elapsed_wall
                    )

                    if effective_speed > 0:
                        self.speed.emit(
                            f"{effective_speed:.2f}x"
                        )

                elif key == "progress" and value == "end":
                    self.progress.emit(100)

            if self.cancel_requested:
                if self.ffmpeg_process.poll() is None:
                    try:
                        self.ffmpeg_process.terminate()
                    except Exception:
                        pass

                try:
                    self.ffmpeg_process.wait(
                        timeout=5
                    )
                except subprocess.TimeoutExpired:
                    try:
                        self.ffmpeg_process.kill()
                    except Exception:
                        pass

                stderr_thread.join(
                    timeout=2
                )

                if os.path.exists(
                    self.output_file
                ):
                    try:
                        os.remove(
                            self.output_file
                        )
                    except Exception:
                        pass

                self.cancelled.emit()
                return

            return_code = self.ffmpeg_process.wait()

            stderr_thread.join(
                timeout=2
            )

            if return_code != 0:
                error_output = "".join(
                    self.stderr_lines
                ).strip()

                raise Exception(
                    error_output
                    or "FFmpeg conversion failed."
                )

            self.progress.emit(100)
            self.eta.emit("00:00")
            self.speed.emit("Done")
            self.status.emit(
                "Conversion complete! ✓"
            )

            self.finished.emit(
                self.output_file
            )

        except Exception as e:
            if self.cancel_requested:
                self.cancelled.emit()
            else:
                self.error.emit(
                    str(e)
                )

        finally:
            self.ffmpeg_process = None


# ============================================================
# APPLE-STYLE INTERACTIVE UI
# ============================================================

class AppleLineEdit(QLineEdit):
    """A quiet, native-feeling field with animated focus/hover treatment."""
    focusProgress = Property(float, lambda self: self._focus, lambda self, v: self._set_focus(v))
    hoverProgress = Property(float, lambda self: self._hover, lambda self, v: self._set_hover(v))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._focus = 0.0
        self._hover = 0.0
        self._focus_anim = None
        self._hover_anim = None
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setClearButtonEnabled(True)

    def _set_focus(self, value):
        self._focus = float(value)
        self.update()

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def _animate(self, prop, current, target, duration):
        anim = QPropertyAnimation(self, prop, self)
        anim.setDuration(duration)
        anim.setStartValue(current)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        return anim

    def enterEvent(self, event):
        if self._hover_anim:
            self._hover_anim.stop()
        self._hover_anim = self._animate(b"hoverProgress", self._hover, 1.0, 150)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._hover_anim:
            self._hover_anim.stop()
        self._hover_anim = self._animate(b"hoverProgress", self._hover, 0.0, 190)
        super().leaveEvent(event)

    def focusInEvent(self, event):
        if self._focus_anim:
            self._focus_anim.stop()
        self._focus_anim = self._animate(b"focusProgress", self._focus, 1.0, 180)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        if self._focus_anim:
            self._focus_anim.stop()
        self._focus_anim = self._animate(b"focusProgress", self._focus, 0.0, 220)
        super().focusOutEvent(event)


class AppleProgressBar(QProgressBar):
    """Painted capsule progress bar with a moving highlight while active."""
    shineProgress = Property(float, lambda self: self._shine, lambda self, v: self._set_shine(v))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shine = 0.0
        self._shine_anim = None
        self.setTextVisible(False)
        self.setMinimumHeight(7)
        self.setMaximumHeight(7)

    def _set_shine(self, value):
        self._shine = float(value)
        self.update()

    def _start_shine(self):
        if self._shine_anim and self._shine_anim.state() == QAbstractAnimation.Running:
            return
        self._shine_anim = QPropertyAnimation(self, b"shineProgress", self)
        self._shine_anim.setDuration(1200)
        self._shine_anim.setStartValue(0.0)
        self._shine_anim.setEndValue(1.0)
        self._shine_anim.setEasingCurve(QEasingCurve.Linear)
        self._shine_anim.setLoopCount(-1)
        self._shine_anim.start()

    def _stop_shine(self):
        if self._shine_anim:
            self._shine_anim.stop()
        self._shine = 0.0
        self.update()

    def setValue(self, value):
        super().setValue(value)
        if 0 < self.value() < 100:
            self._start_shine()
        else:
            self._stop_shine()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#22242A"))
        painter.drawRoundedRect(rect, 3.5, 3.5)

        ratio = 0.0 if self.maximum() == self.minimum() else (
            (self.value() - self.minimum()) / float(self.maximum() - self.minimum())
        )
        fill_w = max(0.0, (rect.width() * ratio))
        if fill_w <= 0.0:
            return

        fill = QRectF(rect.left(), rect.top(), fill_w, rect.height())
        path = QRectF(fill).toRect()
        if fill.width() < 7:
            radius = min(fill.height()/2.0, fill.width()/2.0)
        else:
            radius = 3.5

        gradient = QLinearGradient(fill.left(), 0, fill.right(), 0)
        gradient.setColorAt(0.0, QColor("#0A84FF"))
        gradient.setColorAt(0.72, QColor("#1D8DFF"))
        gradient.setColorAt(1.0, QColor("#42A5FF"))
        painter.setBrush(gradient)
        painter.drawRoundedRect(fill, radius, radius)

        if 0 < self.value() < 100:
            painter.save()
            clip = QRectF(fill.left(), 0, fill.width(), self.height())
            painter.setClipRect(clip)
            x = fill.left() + (fill.width() + 60.0) * self._shine - 30.0
            glow = QRadialGradient(x, self.height() / 2.0, 42.0)
            glow.setColorAt(0.0, QColor(255, 255, 255, 65))
            glow.setColorAt(0.55, QColor(255, 255, 255, 20))
            glow.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(glow)
            painter.drawRoundedRect(fill, radius, radius)
            painter.restore()

class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self._hover = 0.0
        self._mouse = QPoint(0, 0)
        self._hover_anim = None

    hoverProgress = Property(float, lambda self: self._hover, lambda self, v: self._set_hover(v))

    def _set_hover(self, value):
        self._hover = float(value)
        self.update()

    def _animate(self, target):
        if self._hover_anim:
            self._hover_anim.stop()
        self._hover_anim = QPropertyAnimation(self, b"hoverProgress", self)
        self._hover_anim.setDuration(170 if target else 220)
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(target)
        self._hover_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._hover_anim.start()

    def enterEvent(self, event):
        self._animate(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate(0.0)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        self._mouse = event.position().toPoint()
        self.update()
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor("#1A1B1F"))
        gradient.setColorAt(0.55, QColor("#18191C"))
        gradient.setColorAt(1.0, QColor("#16171A"))
        painter.setBrush(gradient)
        border = AppleButton._mix(QColor("#292C31"), QColor("#3B4048"), self._hover * 0.55)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(rect, 20, 20)

        painter.setPen(QPen(QColor(255, 255, 255, 12), 1.0))
        painter.drawLine(18, 1.4, self.width() - 18, 1.4)


class HoverExpandHost(QWidget):
    """A layout-safe interactive card host.

    The card itself never gets transformed or squeezed. Instead the host
    smoothly grows in height while the surrounding layout naturally pushes
    the cards below it down. That creates the tactile "zoom the panel" feel
    without the clipping/shrinking glitches caused by painter scaling.
    """
    hoverProgress = Property(float, lambda s: s._hover, lambda s, v: s._set_hover(v))

    def __init__(self, card, extra_height=12, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.card = card
        self.card.setParent(self)
        self.card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._hover = 0.0
        self._extra = float(extra_height)
        self._base_height = max(1, int(self.card.sizeHint().height()))
        self._anim = None
        self._polling = False

        self.setFixedHeight(self._base_height)
        self.card.installEventFilter(self)
        self._install_child_filters(self.card)
        self._layout_card()

    def _install_child_filters(self, widget):
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def _set_hover(self, value):
        self._hover = float(value)
        self.setFixedHeight(int(round(self._base_height + self._extra * self._hover)))
        self._layout_card()
        self.update()

    def _layout_card(self):
        inset = 2.5 * (1.0 - self._hover)
        self.card.setGeometry(
            int(round(inset)),
            int(round(inset)),
            max(1, int(round(self.width() - inset * 2.0))),
            max(1, int(round(self.height() - inset * 2.0))),
        )

    def resizeEvent(self, event):
        self._layout_card()
        super().resizeEvent(event)

    def _animate(self, target):
        if self._anim:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"hoverProgress", self)
        # Smooth editorial-style easing: soft acceleration/deceleration instead
        # of a linear-looking jump when the layout reflows underneath.
        self._anim.setDuration(220 if target > 0 else 260)
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.setEasingCurve(
            QEasingCurve.OutCubic if target > 0 else QEasingCurve.InOutCubic
        )
        self._anim.start()

    def _sync_hover(self):
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        target = 1.0 if inside else 0.0
        if abs(target - self._hover) > 0.01:
            self._animate(target)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Enter, QEvent.Leave):
            QTimer.singleShot(0, self._sync_hover)
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        # Re-measure once the widget has a real style/layout context.
        QTimer.singleShot(0, self._refresh_base_height)
        super().showEvent(event)

    def _refresh_base_height(self):
        if self._hover < 0.01:
            hint = self.card.sizeHint().height()
            if hint > 1:
                self._base_height = max(self._base_height, int(hint))
                self.setFixedHeight(self._base_height)
                self._layout_card()


class StatusPill(QLabel):
    def __init__(self, text="Ready", parent=None):
        super().__init__(text, parent)
        self.setObjectName("statusPill")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(28)
        self.setMinimumWidth(82)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._state = "idle"
        self._pulse = 0.0
        self._anim = None

    pulse = Property(float, lambda self: self._pulse, lambda self, v: self._set_pulse(v))

    def _set_pulse(self, value):
        self._pulse = float(value)
        self.update()

    def set_state(self, state, text):
        self._state = state
        self.setText(text)
        if self._anim:
            self._anim.stop()
        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setDuration(320)
        self._anim.setStartValue(0.0)
        self._anim.setKeyValueAt(0.32, 1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.start()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0.8, 0.8, self.width() - 1.6, self.height() - 1.6)

        colors = {
            "idle": (QColor("#1A1A1E"), QColor("#303037"), QColor("#A5A5AE")),
            "working": (QColor("#11243A"), QColor("#244D7A"), QColor("#72B7FF")),
            "success": (QColor("#13251C"), QColor("#2A5A3E"), QColor("#7BDD9F")),
            "error": (QColor("#281619"), QColor("#5E2B30"), QColor("#FF817A")),
        }
        bg, border, text_color = colors.get(self._state, colors["idle"])

        painter.setBrush(bg)
        painter.setPen(QPen(border, 1.0))
        painter.drawRoundedRect(rect, 13, 13)

        if self._pulse > 0.01:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, int(65 * self._pulse)), 1.0))
            painter.drawRoundedRect(rect.adjusted(1.0, 1.0, -1.0, -1.0), 12, 12)

        painter.setFont(self.font())
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignCenter, self.text())


class AppleMenuItem(QPushButton):
    """Animated rounded popover row."""
    hoverProgress = Property(float, lambda self: self._hover, lambda self, v: self._set_hover(v))
    pressProgress = Property(float, lambda self: self._press, lambda self, v: self._set_press(v))
    selectedProgress = Property(float, lambda self: self._selected, lambda self, v: self._set_selected(v))

    def __init__(self, text, selected=False, parent=None):
        super().__init__(text, parent)
        self._selected = 1.0 if selected else 0.0
        self._hover = 0.0
        self._press = 0.0
        self._hover_anim = None
        self._press_anim = None
        self._selected_anim = None
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(36)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setStyleSheet("QPushButton { background: transparent; border: 0; padding: 0; }")

    def _set_hover(self, value):
        self._hover = float(value); self.update()
    def _set_press(self, value):
        self._press = float(value); self.update()
    def _set_selected(self, value):
        self._selected = float(value); self.update()

    def _animate(self, prop, current, target, duration):
        anim=QPropertyAnimation(self, prop, self)
        anim.setDuration(duration); anim.setStartValue(current); anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic); anim.start(); return anim

    def set_selected(self, selected):
        if self._selected_anim: self._selected_anim.stop()
        self._selected_anim=self._animate(b"selectedProgress", self._selected, 1.0 if selected else 0.0, 180)

    def enterEvent(self,event):
        if self._hover_anim: self._hover_anim.stop()
        self._hover_anim=self._animate(b"hoverProgress", self._hover, 1.0, 130)
        super().enterEvent(event)
    def leaveEvent(self,event):
        if self._hover_anim: self._hover_anim.stop()
        self._hover_anim=self._animate(b"hoverProgress", self._hover, 0.0, 180)
        if self._press_anim: self._press_anim.stop()
        self._press_anim=self._animate(b"pressProgress", self._press, 0.0, 100)
        super().leaveEvent(event)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:
            if self._press_anim: self._press_anim.stop()
            self._press_anim=self._animate(b"pressProgress", self._press, 1.0, 60)
        super().mousePressEvent(event)
    def mouseReleaseEvent(self,event):
        super().mouseReleaseEvent(event)
        if self._press_anim: self._press_anim.stop()
        self._press_anim=self._animate(b"pressProgress", self._press, 0.0, 120)

    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing,True); painter.setRenderHint(QPainter.TextAntialiasing,True)
        rect=QRectF(2,1,self.width()-4,self.height()-2)
        base=QColor(255,255,255,0)
        selected=QColor("#2B2C31")
        hover=QColor("#34363B")
        bg=AppleButton._mix(base, selected, min(1.0,self._selected*0.72))
        bg=AppleButton._mix(bg, hover, self._hover*0.75)
        bg=AppleButton._mix(bg, QColor("#24252A"), self._press*0.35)
        painter.setBrush(bg); painter.setPen(Qt.NoPen); painter.drawRoundedRect(rect,10,10)
        font=self.font(); font.setWeight(QFont.Weight.Medium)
        painter.setFont(font); painter.setPen(QColor("#F5F5F7"))
        painter.drawText(rect.adjusted(12,0,-38,0),Qt.AlignVCenter|Qt.AlignLeft,self.text())
        if self._selected>0.01:
            alpha=int(210*self._selected)
            pen=QPen(QColor(255,255,255,alpha),1.8); pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin); painter.setPen(pen)
            x=self.width()-19; y=self.height()/2
            painter.drawLine(x-4,y,x-1,y+3); painter.drawLine(x-1,y+3,x+5,y-4)


class AppleComboPopup(QFrame):
    """A custom rounded transient popup. No native combo frame is involved."""
    item_selected = Signal(int)
    closed = Signal()

    def __init__(self, combo, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.combo = combo
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setObjectName("applePopover")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(shadow)
        self.items = []
        self._target_geometry = QRect()
        self._animation = None
        self._build()

    def _build(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(6, 6, 6, 6)
        self.layout.setSpacing(2)

    def rebuild(self):
        while self.layout.count():
            item = self.layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.items = []
        for i, text in enumerate(self.combo._items):
            row = AppleMenuItem(text, i == self.combo._index, self)
            row.clicked.connect(lambda checked=False, idx=i: self._choose(idx))
            self.layout.addWidget(row)
            self.items.append(row)
        width = max(self.combo.width(), 220)
        fm = self.combo.fontMetrics()
        longest = max([fm.horizontalAdvance(x) for x in self.combo._items] + [0])
        width = max(width, longest + 74)
        height = 12 + len(self.combo._items) * 36
        self.setFixedSize(width, height)

    def _choose(self, index):
        self.item_selected.emit(index)
        self.close()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0.6, 0.6, self.width() - 1.2, self.height() - 1.2)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0.0, QColor("#242529"))
        gradient.setColorAt(1.0, QColor("#1C1D20"))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor("#3B3D43"), 1.0))
        painter.drawRoundedRect(rect, 13, 13)
        painter.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
        painter.drawLine(14, 1.4, self.width() - 14, 1.4)

    def show_animated(self, target_geometry):
        self._target_geometry = target_geometry
        self.setGeometry(target_geometry)
        self.show()
        self.raise_()
        self.activateWindow()

        start_w = max(40, int(target_geometry.width() * 0.92))
        start_h = max(30, int(target_geometry.height() * 0.92))
        start_x = target_geometry.x() + (target_geometry.width() - start_w) // 2
        start_y = target_geometry.y() + 7
        start = QRect(start_x, start_y, start_w, start_h)

        self.setGeometry(start)
        self._animation = QPropertyAnimation(self, b"geometry", self)
        self._animation.setDuration(210)
        self._animation.setStartValue(start)
        self._animation.setEndValue(target_geometry)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.start()


class AppleComboBox(QFrame):
    """Custom Apple-like select with tactile hover and rotating chevron."""
    currentTextChanged = Signal(str)
    currentIndexChanged = Signal(int)

    def __init__(self,parent=None):
        super().__init__(parent)
        self._items=[]; self._index=-1; self._hover=0.0; self._press=0.0; self._open=0.0
        self._mouse=QPoint(0,0); self._hover_anim=None; self._press_anim=None; self._open_anim=None; self._popup=None
        self.setCursor(Qt.PointingHandCursor); self.setMouseTracking(True); self.setAttribute(Qt.WA_Hover,True); self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedHeight(42); self.setObjectName("appleCombo")

    hoverProgress=Property(float,lambda s:s._hover,lambda s,v:s._set_hover(v))
    pressProgress=Property(float,lambda s:s._press,lambda s,v:s._set_press(v))
    openProgress=Property(float,lambda s:s._open,lambda s,v:s._set_open(v))
    def _set_hover(self,v): self._hover=float(v); self.update()
    def _set_press(self,v): self._press=float(v); self.update()
    def _set_open(self,v): self._open=float(v); self.update()
    def _animate(self,prop,current,target,duration,easing=QEasingCurve.OutCubic):
        a=QPropertyAnimation(self,prop,self); a.setDuration(duration); a.setStartValue(current); a.setEndValue(target); a.setEasingCurve(easing); a.start(); return a
    def addItems(self,items):
        for x in items: self.addItem(x)
    def addItem(self,text):
        self._items.append(str(text)); self._index=0 if self._index==-1 else self._index; self.update()
    def clear(self): self._items.clear(); self._index=-1; self.update()
    def count(self): return len(self._items)
    def itemText(self,index): return self._items[index] if 0<=index<len(self._items) else ""
    def currentIndex(self): return self._index
    def currentText(self): return self._items[self._index] if 0<=self._index<len(self._items) else ""
    def setCurrentText(self,text):
        if str(text) in self._items: self.setCurrentIndex(self._items.index(str(text)))
    def setCurrentIndex(self,index):
        index=int(index)
        if not (0<=index<len(self._items)) or index==self._index: return
        self._index=index; self.update(); self.currentIndexChanged.emit(index); self.currentTextChanged.emit(self.currentText())
    def enterEvent(self,event):
        if self._hover_anim: self._hover_anim.stop()
        self._hover_anim=self._animate(b"hoverProgress",self._hover,1.0,190,QEasingCurve.OutCubic); super().enterEvent(event)
    def leaveEvent(self,event):
        if self._hover_anim: self._hover_anim.stop()
        self._hover_anim=self._animate(b"hoverProgress",self._hover,0.0,230,QEasingCurve.InOutCubic); super().leaveEvent(event)
    def mouseMoveEvent(self,event): self._mouse=event.position().toPoint(); self.update(); super().mouseMoveEvent(event)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self.isEnabled():
            if self._press_anim: self._press_anim.stop()
            self._press_anim=self._animate(b"pressProgress",self._press,1.0,65); self.showPopup()
        super().mousePressEvent(event)
    def mouseReleaseEvent(self,event):
        if self._press_anim: self._press_anim.stop()
        self._press_anim=self._animate(b"pressProgress",self._press,0.0,120); super().mouseReleaseEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space): self.showPopup(); return
        if event.key()==Qt.Key_Down and self._items: self.setCurrentIndex(min(self._index+1,len(self._items)-1)); return
        if event.key()==Qt.Key_Up and self._items: self.setCurrentIndex(max(self._index-1,0)); return
        super().keyPressEvent(event)
    def _popup_closed(self):
        if self._open_anim: self._open_anim.stop()
        self._open_anim=self._animate(b"openProgress",self._open,0.0,170)
    def showPopup(self):
        if not self._items or not self.isEnabled(): return
        if self._popup is not None: self._popup.close()
        popup=AppleComboPopup(self); popup.rebuild(); popup.item_selected.connect(self.setCurrentIndex); popup.closed.connect(self._popup_closed)
        below=self.mapToGlobal(QPoint(0,self.height()+7)); screen=QApplication.screenAt(below) or QApplication.primaryScreen(); available=screen.availableGeometry() if screen else QRect(0,0,1440,900)
        x=min(max(available.left()+8,below.x()),available.right()-popup.width()-8); y=below.y()
        if y+popup.height()>available.bottom()-8: y=self.mapToGlobal(QPoint(0,-popup.height()-7)).y()
        target=QRect(x,y,popup.width(),popup.height()); self._popup=popup; popup.show_animated(target)
        if self._open_anim: self._open_anim.stop()
        self._open_anim=self._animate(b"openProgress",self._open,1.0,180,QEasingCurve.OutBack)
    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing,True); painter.setRenderHint(QPainter.TextAntialiasing,True)
        if not self.isEnabled(): bg=QColor("#151619"); border=QColor("#282A2E"); text=QColor("#656870")
        elif self._open>0.01: bg=QColor("#1D1F23"); border=QColor("#4C515A"); text=QColor("#F5F5F7")
        elif self._hover: bg=QColor("#1B1C20"); border=QColor("#3E4148"); text=QColor("#F5F5F7")
        else: bg=QColor("#17181B"); border=QColor("#2D3035"); text=QColor("#F2F2F4")
        rect=QRectF(0.8,0.8,self.width()-1.6,self.height()-1.6)
        painter.setBrush(bg); painter.setPen(QPen(border,1.0)); painter.drawRoundedRect(rect,13,13)
        if self._hover>0.01:
            painter.save(); path=QPainterPath(); path.addRoundedRect(rect.adjusted(1,1,-1,-1),12,12); painter.setClipPath(path)
            rg=QRadialGradient(self._mouse.x(),self._mouse.y(),100); rg.setColorAt(0.0,QColor(255,255,255,int(23*self._hover))); rg.setColorAt(1.0,QColor(255,255,255,0)); painter.setBrush(rg); painter.setPen(Qt.NoPen); painter.drawRect(rect); painter.restore()
        painter.setPen(text); painter.setFont(self.font()); painter.drawText(rect.adjusted(13,0,-38,0),Qt.AlignVCenter|Qt.AlignLeft,self.currentText())
        cx=self.width()-18; cy=self.height()/2.0
        painter.save(); painter.translate(cx,cy); painter.rotate(180*self._open); pen=QPen(QColor("#AEB1B9") if self._hover else QColor("#81858D"),1.7); pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin); painter.setPen(pen); painter.drawLine(-4,-2,0,2); painter.drawLine(0,2,4,-2); painter.restore()
        if self._press>0.01:
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(QColor(255,255,255,int(20*(1-self._press))),1.0)); painter.drawRoundedRect(rect.adjusted(1.2,1.2,-1.2,-1.2),12,12)


class AppleButton(QPushButton):
    """Stable Apple-like button with a visible, safe tactile bounce.

    The button stays entirely QSS/native-rendered.  The click animation does
    not resize or reposition the widget, so layouts remain stable.  Instead,
    it animates the content's vertical travel together with the drop shadow,
    producing a small physical press -> rebound -> settle motion.
    """

    @staticmethod
    def _mix(a, b, t):
        t = max(0.0, min(1.0, float(t)))
        return QColor(
            int(a.red() + (b.red() - a.red()) * t),
            int(a.green() + (b.green() - a.green()) * t),
            int(a.blue() + (b.blue() - a.blue()) * t),
            int(a.alpha() + (b.alpha() - a.alpha()) * t),
        )

    shadowAlpha = Property(
        float,
        lambda self: float(self._shadow.color().alpha()),
        lambda self, value: self._set_shadow_alpha(value),
    )

    shadowY = Property(
        float,
        lambda self: float(self._shadow.yOffset()),
        lambda self, value: self._set_shadow_y(value),
    )

    pressOffset = Property(
        float,
        lambda self: float(self._press_offset),
        lambda self, value: self._set_press_offset(value),
    )

    def __init__(self, text="", parent=None):
        # Keep the QPushButton itself as the stable layout/hit target.
        # A child label provides a safe, visible motion target for the click.
        self._label_ready = False
        super().__init__(text, parent)
        initial_text = super().text()
        super().setText("")

        self._base_margins = self.contentsMargins()
        self._press_offset = 0.0
        self._bounce_anim = None
        self._shadow_anim = None

        self._content_label = QLabel(initial_text, self)
        self._content_label.setObjectName("buttonContent")
        self._content_label.setAlignment(Qt.AlignCenter)
        self._content_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._content_label.setStyleSheet("background: transparent; border: 0;")
        self._label_ready = True

        self._pop_overlay = QFrame(self)
        self._pop_overlay.setObjectName("buttonPopOverlay")
        self._pop_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._pop_overlay.hide()
        self._pop_anim = None

        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(0.0)
        self._shadow.setOffset(0.0, 0.0)
        self._shadow.setColor(QColor(0, 0, 0, 0))
        self.setGraphicsEffect(self._shadow)

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setFlat(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def setMinimumHeight(self, height):
        super().setMinimumHeight(max(1, int(height)))

    def setFixedHeight(self, height):
        super().setFixedHeight(max(1, int(height)))

    def _set_shadow_alpha(self, value):
        color = self._shadow.color()
        color.setAlpha(max(0, min(255, int(value))))
        self._shadow.setColor(color)

    def _set_shadow_y(self, value):
        self._shadow.setOffset(0.0, float(value))

    def _set_press_offset(self, value):
        self._press_offset = float(value)
        if not getattr(self, "_label_ready", False):
            return
        self._layout_button_content(int(round(self._press_offset)))

    def _layout_button_content(self, shift=0):
        if not hasattr(self, "_content_label"):
            return
        inset = 1
        self._content_label.setGeometry(
            inset,
            inset + int(shift),
            max(1, self.width() - inset * 2),
            max(1, self.height() - inset * 2),
        )
        self._content_label.setFont(self.font())

    def resizeEvent(self, event):
        self._layout_button_content(int(round(self._press_offset)))
        if hasattr(self, "_pop_overlay"):
            self._pop_overlay.setGeometry(1, 1, max(1, self.width() - 2), max(1, self.height() - 2))
        super().resizeEvent(event)

    def setText(self, text):
        if getattr(self, "_label_ready", False):
            self._content_label.setText(text)
        else:
            super().setText(text)

    def text(self):
        if getattr(self, "_label_ready", False):
            return self._content_label.text()
        return super().text()

    def _pop_style(self):
        if self.objectName() == "primaryButton":
            return ("border: 1px solid rgba(255,255,255,150); border-radius: 12px; "
                    "background: rgba(255,255,255,28);")
        if self.objectName() == "dangerButton":
            return ("border: 1px solid rgba(255,145,170,135); border-radius: 12px; "
                    "background: rgba(255,145,170,18);")
        return ("border: 1px solid rgba(255,255,255,105); border-radius: 12px; "
                "background: rgba(255,255,255,15);")

    def _start_pop(self):
        """Soft click highlight; no snap/overshoot geometry jump."""
        if not self.isEnabled():
            return
        if self._pop_anim:
            self._pop_anim.stop()

        self._pop_overlay.setStyleSheet(self._pop_style())
        self._pop_overlay.raise_()
        self._pop_overlay.show()

        w, h = self.width(), self.height()
        start = QRect(2, 2, max(1, w - 4), max(1, h - 4))
        pressed = QRect(1, 1, max(1, w - 2), max(1, h - 2))
        settle = QRect(2, 2, max(1, w - 4), max(1, h - 4))

        seq = QSequentialAnimationGroup(self)

        press = QPropertyAnimation(self._pop_overlay, b"geometry", seq)
        press.setDuration(75)
        press.setStartValue(start)
        press.setEndValue(pressed)
        press.setEasingCurve(QEasingCurve.InCubic)

        release = QPropertyAnimation(self._pop_overlay, b"geometry", seq)
        release.setDuration(220)
        release.setStartValue(pressed)
        release.setEndValue(settle)
        release.setEasingCurve(QEasingCurve.OutCubic)

        seq.addAnimation(press)
        seq.addAnimation(release)
        seq.finished.connect(self._pop_overlay.hide)
        self._pop_anim = seq
        seq.start()

    def _animate_shadow(self, blur, alpha, y, duration, easing):
        if self._shadow_anim:
            self._shadow_anim.stop()

        group = QParallelAnimationGroup(self)

        a = QPropertyAnimation(self._shadow, b"blurRadius", group)
        a.setDuration(duration)
        a.setStartValue(float(self._shadow.blurRadius()))
        a.setEndValue(float(blur))
        a.setEasingCurve(easing)
        group.addAnimation(a)

        b = QPropertyAnimation(self, b"shadowAlpha", group)
        b.setDuration(duration)
        b.setStartValue(float(self._shadow.color().alpha()))
        b.setEndValue(float(alpha))
        b.setEasingCurve(easing)
        group.addAnimation(b)

        c = QPropertyAnimation(self, b"shadowY", group)
        c.setDuration(duration)
        c.setStartValue(float(self._shadow.yOffset()))
        c.setEndValue(float(y))
        c.setEasingCurve(easing)
        group.addAnimation(c)

        self._shadow_anim = group
        group.start()

    def enterEvent(self, event):
        if self.isEnabled():
            self._animate_shadow(
                10.0, 30.0, 1.5, 145,
                QEasingCurve.OutCubic
            )
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_shadow(
            0.0, 0.0, 0.0, 190,
            QEasingCurve.OutCubic
        )
        super().leaveEvent(event)

    def _start_bounce(self):
        """A clean, editable-feeling press/release curve: InCubic down, OutCubic up."""
        if self._bounce_anim:
            self._bounce_anim.stop()

        group = QSequentialAnimationGroup(self)

        press = QParallelAnimationGroup(group)

        offset = QPropertyAnimation(self, b"pressOffset", press)
        offset.setDuration(78)
        offset.setStartValue(float(self._press_offset))
        offset.setEndValue(1.8)
        offset.setEasingCurve(QEasingCurve.InCubic)
        press.addAnimation(offset)

        shadow_y = QPropertyAnimation(self, b"shadowY", press)
        shadow_y.setDuration(78)
        shadow_y.setStartValue(float(self._shadow.yOffset()))
        shadow_y.setEndValue(0.6)
        shadow_y.setEasingCurve(QEasingCurve.InCubic)
        press.addAnimation(shadow_y)

        shadow_blur = QPropertyAnimation(self._shadow, b"blurRadius", press)
        shadow_blur.setDuration(78)
        shadow_blur.setStartValue(float(self._shadow.blurRadius()))
        shadow_blur.setEndValue(4.0)
        shadow_blur.setEasingCurve(QEasingCurve.InCubic)
        press.addAnimation(shadow_blur)

        release = QParallelAnimationGroup(group)

        release_offset = QPropertyAnimation(self, b"pressOffset", release)
        release_offset.setDuration(230)
        release_offset.setStartValue(1.8)
        release_offset.setEndValue(0.0)
        release_offset.setEasingCurve(QEasingCurve.OutCubic)
        release.addAnimation(release_offset)

        release_y = QPropertyAnimation(self, b"shadowY", release)
        release_y.setDuration(230)
        release_y.setStartValue(0.6)
        release_y.setEndValue(1.5 if self.underMouse() else 0.0)
        release_y.setEasingCurve(QEasingCurve.OutCubic)
        release.addAnimation(release_y)

        release_blur = QPropertyAnimation(self._shadow, b"blurRadius", release)
        release_blur.setDuration(230)
        release_blur.setStartValue(4.0)
        release_blur.setEndValue(10.0 if self.underMouse() else 0.0)
        release_blur.setEasingCurve(QEasingCurve.OutCubic)
        release.addAnimation(release_blur)

        group.addAnimation(press)
        group.addAnimation(release)
        self._bounce_anim = group
        group.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled():
            self._start_bounce()
            self._start_pop()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)


class SidebarButton(AppleButton):
    def __init__(self, icon, text, parent=None):
        super().__init__(f"{icon}   {text}", parent)
        self.setObjectName("sidebarButton")
        self.setCheckable(True)
        self.setMinimumHeight(44)
        self.setMaximumHeight(44)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


class SidebarNav(QWidget):
    page_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarNav")
        self.setFixedHeight(104)
        self._indicator_anim = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        self.downloader = SidebarButton("↓", "Downloader")
        self.converter = SidebarButton("↔", "Converter")
        layout.addWidget(self.downloader)
        layout.addWidget(self.converter)

        self.indicator = QFrame(self)
        self.indicator.setObjectName("sidebarIndicator")
        self.indicator.setAttribute(Qt.WA_StyledBackground, True)
        self.indicator.lower()
        QTimer.singleShot(0, self._place_indicator)

        self.downloader.clicked.connect(lambda: self._request_page(0))
        self.converter.clicked.connect(lambda: self._request_page(1))
        self.downloader.setChecked(True)

    def _place_indicator(self):
        button = self.downloader if self.downloader.isChecked() else self.converter
        self.indicator.setGeometry(button.geometry())

    def _request_page(self, index):
        self.page_requested.emit(index)

    def set_index(self, index):
        self.downloader.setChecked(index == 0)
        self.converter.setChecked(index == 1)
        target_button = self.downloader if index == 0 else self.converter
        target = target_button.geometry()
        if self._indicator_anim:
            self._indicator_anim.stop()
        self._indicator_anim = QPropertyAnimation(self.indicator, b"geometry", self)
        self._indicator_anim.setDuration(260)
        self._indicator_anim.setStartValue(self.indicator.geometry())
        self._indicator_anim.setEndValue(target)
        self._indicator_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._indicator_anim.start()


class DropIcon(QLabel):
    """Simple native-rendered upload icon; no custom QPainter path."""
    def __init__(self, parent=None):
        super().__init__("↓", parent)
        self.setFixedSize(46, 46)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setObjectName("dropIcon")


class DropZone(QFrame):
    file_dropped=Signal(str); clicked=Signal()
    dragProgress=Property(float,lambda s:s._drag_progress,lambda s,v:s._set_drag_progress(v)); hoverProgress=Property(float,lambda s:s._hover_progress,lambda s,v:s._set_hover_progress(v)); dashProgress=Property(float,lambda s:s._dash,lambda s,v:s._set_dash(v))
    def __init__(self,parent=None):
        super().__init__(parent); self.setObjectName("dropZone"); self.setAcceptDrops(True); self.setMouseTracking(True); self.setAttribute(Qt.WA_Hover,True); self.setAttribute(Qt.WA_TranslucentBackground,True); self.setMinimumHeight(146); self.setMaximumHeight(146); self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self._drag_progress=0.0; self._hover_progress=0.0; self._dash=0.0; self._mouse=QPoint(0,0); self._drag_anim=None; self._hover_anim=None; self._dash_anim=None
        layout=QVBoxLayout(self); layout.setAlignment(Qt.AlignCenter); layout.setSpacing(5); layout.setContentsMargins(22,16,22,16)
        self.icon=DropIcon(); self.icon.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        title=QLabel("Add files"); title.setObjectName("dropTitle"); title.setAlignment(Qt.AlignCenter); title.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        subtitle=QLabel("Drop a media file here or click to browse"); subtitle.setObjectName("dropSubtitle"); subtitle.setAlignment(Qt.AlignCenter); subtitle.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        types=QLabel("MP4  •  MKV  •  MOV  •  WebM  •  MP3  •  WAV"); types.setObjectName("dropTypes"); types.setAlignment(Qt.AlignCenter); types.setAttribute(Qt.WA_TransparentForMouseEvents,True)
        layout.addWidget(self.icon,0,Qt.AlignCenter); layout.addWidget(title); layout.addWidget(subtitle); layout.addWidget(types)
    def _set_drag_progress(self,v): self._drag_progress=float(v); self.icon.progress=self._drag_progress; self.update()
    def _set_hover_progress(self,v): self._hover_progress=float(v); self.icon.hover=self._hover_progress; self.update()
    def _set_dash(self,v): self._dash=float(v); self.update()
    def _animate(self,prop,current,target,duration):
        a=QPropertyAnimation(self,prop,self); a.setDuration(duration); a.setStartValue(current); a.setEndValue(target); a.setEasingCurve(QEasingCurve.OutCubic); a.start(); return a
    def _start_dash(self):
        if self._dash_anim and self._dash_anim.state()==QAbstractAnimation.Running: return
        self._dash_anim=QPropertyAnimation(self,b"dashProgress",self); self._dash_anim.setDuration(900); self._dash_anim.setStartValue(0.0); self._dash_anim.setEndValue(1.0); self._dash_anim.setEasingCurve(QEasingCurve.Linear); self._dash_anim.setLoopCount(-1); self._dash_anim.start()
    def _stop_dash(self):
        if self._dash_anim: self._dash_anim.stop()
        self._dash=0.0; self.update()
    def enterEvent(self,event):
        if self._hover_anim:self._hover_anim.stop()
        self._hover_anim=self._animate(b"hoverProgress",self._hover_progress,1.0,170); super().enterEvent(event)
    def leaveEvent(self,event):
        if self._hover_anim:self._hover_anim.stop()
        self._hover_anim=self._animate(b"hoverProgress",self._hover_progress,0.0,220); super().leaveEvent(event)
    def mouseMoveEvent(self,event): self._mouse=event.position().toPoint(); self.update(); super().mouseMoveEvent(event)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self.isEnabled(): self.clicked.emit()
        super().mousePressEvent(event)
    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            if self._drag_anim:self._drag_anim.stop()
            self._drag_anim=self._animate(b"dragProgress",self._drag_progress,1.0,180); self._start_dash(); event.acceptProposedAction(); return
        event.ignore()
    def dragLeaveEvent(self,event):
        if self._drag_anim:self._drag_anim.stop()
        self._drag_anim=self._animate(b"dragProgress",self._drag_progress,0.0,210); self._stop_dash(); event.accept()
    def dropEvent(self,event):
        if self._drag_anim:self._drag_anim.stop()
        self._drag_anim=self._animate(b"dragProgress",self._drag_progress,0.0,180); self._stop_dash()
        for url in event.mimeData().urls():
            if url.isLocalFile(): self.file_dropped.emit(url.toLocalFile()); event.acceptProposedAction(); return
        event.ignore()
    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing,True); rect=QRectF(1,1,self.width()-2,self.height()-2); hover=self._hover_progress; drag=self._drag_progress
        bg=AppleButton._mix(QColor("#121216"),QColor("#17191E"),hover); bg=AppleButton._mix(bg,QColor("#10243A"),drag)
        border=AppleButton._mix(QColor("#303038"),QColor("#575A64"),hover*.6); border=AppleButton._mix(border,QColor("#0A84FF"),drag)
        painter.setBrush(bg); pen=QPen(border,1.0 if drag<.5 else 1.5,Qt.DashLine); pen.setDashPattern([5.5,4.0]); pen.setDashOffset(-10*self._dash); painter.setPen(pen); painter.drawRoundedRect(rect,17,17)
        if drag>0.01:
            painter.setBrush(Qt.NoBrush); painter.setPen(QPen(QColor(10,132,255,int(55*drag)),2.8)); painter.drawRoundedRect(rect.adjusted(2,2,-2,-2),15,15)


class SidebarIndicatorHost(QWidget):
    pass


# ============================================================
# DOWNLOADER
# ============================================================

class DownloaderTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.progress_anim = None
        self._visibility_anims = {}
        self.build_ui()

    def field(self, label):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        text = QLabel(label)
        text.setObjectName("fieldLabel")
        layout.addWidget(text)
        return box, layout

    def build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer.addWidget(scroll)

        page = QWidget()
        page.setObjectName("workspacePage")
        root = QVBoxLayout(page)
        root.setContentsMargins(46, 38, 46, 44)
        root.setSpacing(18)
        scroll.setWidget(page)

        # Header
        eyebrow = QLabel("YOUTUBE")
        eyebrow.setObjectName("eyebrow")
        root.addWidget(eyebrow)

        header = QHBoxLayout()
        header.setSpacing(18)
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("Downloader")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Save a video or extract its audio in a format that fits your workflow.")
        subtitle.setObjectName("pageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status_pill = StatusPill()
        header.addWidget(self.status_pill, 0, Qt.AlignTop)
        root.addLayout(header)

        # Source
        source = Card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(24, 22, 24, 24)
        source_layout.setSpacing(11)
        label_row = QHBoxLayout()
        label = QLabel("Source")
        label.setObjectName("sectionTitle")
        hint = QLabel("Paste a YouTube link")
        hint.setObjectName("cardHint")
        label_row.addWidget(label)
        label_row.addStretch()
        label_row.addWidget(hint)
        source_layout.addLayout(label_row)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.url_input = AppleLineEdit()
        self.url_input.setPlaceholderText("https://youtube.com/watch?v=…")
        self.url_input.setMinimumHeight(48)
        self.url_input.setClearButtonEnabled(True)
        paste = AppleButton("Paste")
        paste.setObjectName("secondaryButton")
        paste.setFixedWidth(92)
        paste.setMinimumHeight(48)
        paste.clicked.connect(self.paste_url)
        row.addWidget(self.url_input, 1)
        row.addWidget(paste)
        source_layout.addLayout(row)
        root.addWidget(HoverExpandHost(source, 14))

        # Output
        settings = Card()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(24, 22, 24, 22)
        settings_layout.setSpacing(16)

        header_row = QHBoxLayout()
        output_title = QLabel("Output")
        output_title.setObjectName("sectionTitle")
        self.hint = QLabel("H.264 is the safest choice for editing.")
        self.hint.setObjectName("cardHint")
        header_row.addWidget(output_title)
        header_row.addStretch()
        header_row.addWidget(self.hint)
        settings_layout.addLayout(header_row)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(13)
        fields.setColumnStretch(0, 1)
        fields.setColumnStretch(1, 1)
        fields.setColumnStretch(2, 1)

        self.format_box, f = self.field("Format")
        self.format_combo = AppleComboBox()
        self.format_combo.addItems(["MP4", "MKV", "WebM", "MP3", "M4A", "WAV", "FLAC", "Opus"])
        f.addWidget(self.format_combo)
        fields.addWidget(self.format_box, 0, 0)

        self.settings_stack = QStackedWidget()
        self.settings_stack.setMinimumHeight(58)

        video_page = QWidget()
        vr = QHBoxLayout(video_page)
        vr.setContentsMargins(0, 0, 0, 0)
        vr.setSpacing(12)
        self.quality_box, q = self.field("Video quality")
        self.quality_combo = AppleComboBox()
        self.quality_combo.addItems(["Best Available", "1080p", "720p", "480p", "360p"])
        q.addWidget(self.quality_combo)
        self.codec_box, c = self.field("Video codec")
        self.codec_combo = AppleComboBox()
        self.codec_combo.addItems(["H.264", "VP9", "AV1", "Any"])
        self.codec_combo.setCurrentIndex(0)
        c.addWidget(self.codec_combo)
        vr.addWidget(self.quality_box, 1)
        vr.addWidget(self.codec_box, 1)
        self.settings_stack.addWidget(video_page)

        audio_page = QWidget()
        ar = QHBoxLayout(audio_page)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(12)
        self.audio_box, a = self.field("Audio quality")
        self.audio_quality_combo = AppleComboBox()
        self.audio_quality_combo.addItems(["320 kbps", "256 kbps", "192 kbps", "128 kbps"])
        a.addWidget(self.audio_quality_combo)
        note = QLabel("FFmpeg audio extraction")
        note.setObjectName("inlineHint")
        ar.addWidget(self.audio_box, 1)
        ar.addWidget(note, 1, Qt.AlignVCenter)
        self.settings_stack.addWidget(audio_page)

        fields.addWidget(self.settings_stack, 0, 1, 1, 2)
        settings_layout.addLayout(fields)

        save = QHBoxLayout()
        save.setSpacing(10)
        self.folder_box, folder_layout = self.field("Save to")
        self.folder_input = QLineEdit(os.path.expanduser("~/Downloads"))
        self.folder_input.setMinimumHeight(44)
        folder_layout.addWidget(self.folder_input)
        save.addWidget(self.folder_box, 1)
        browse = AppleButton("Choose folder")
        browse.setObjectName("secondaryButton")
        browse.setFixedWidth(130)
        browse.setMinimumHeight(44)
        browse.clicked.connect(self.choose_folder)
        save.addWidget(browse)
        settings_layout.addLayout(save)
        root.addWidget(HoverExpandHost(settings, 14))

        # Action
        action = Card()
        action.setObjectName("actionSurface")
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(14, 14, 14, 14)
        action_layout.setSpacing(11)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.download_button = AppleButton("Download")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setMinimumHeight(52)
        self.stop_button = AppleButton("Stop")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setMinimumHeight(52)
        self.stop_button.setFixedWidth(120)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.download_button, 1)
        buttons.addWidget(self.stop_button)
        action_layout.addLayout(buttons)

        status = QHBoxLayout()
        self.status_label = QLabel("Ready to download")
        self.status_label.setObjectName("statusLabel")
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("percentLabel")
        status.addWidget(self.status_label)
        status.addStretch()
        status.addWidget(self.percent_label)
        action_layout.addLayout(status)

        self.progress_bar = AppleProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        action_layout.addWidget(self.progress_bar)

        meta = QHBoxLayout()
        self.speed_label = QLabel("Speed  —")
        self.eta_label = QLabel("ETA  —")
        self.speed_label.setObjectName("metaLabel")
        self.eta_label.setObjectName("metaLabel")
        meta.addWidget(self.speed_label)
        meta.addStretch()
        meta.addWidget(self.eta_label)
        action_layout.addLayout(meta)
        root.addWidget(HoverExpandHost(action, 10))
        root.addStretch(1)

        self.settings_stack.setCurrentIndex(0)
        self.format_combo.currentTextChanged.connect(self.format_changed)
        self.download_button.clicked.connect(self.start_download)
        self.stop_button.clicked.connect(self.stop_download)
        self.status_pill.set_state("idle", "Ready")

    def paste_url(self):
        text = QApplication.clipboard().text().strip()
        if text:
            self.url_input.setText(text)
            self.status_label.setText("URL pasted — ready")

    def _animate_visibility(self, widget, show):
        widget.setVisible(show)

    def format_changed(self, name):
        audio = name in ["MP3", "M4A", "WAV", "FLAC", "Opus"]
        self.settings_stack.setCurrentIndex(1 if audio else 0)
        self.hint.setText(
            "Audio extraction • choose bitrate below."
            if audio
            else "H.264 is the safest default for editors."
        )

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose Download Folder")
        if folder:
            self.folder_input.setText(folder)

    def start_download(self):
        url = self.url_input.text().strip()
        folder = self.folder_input.text().strip()
        if not url:
            self.status_label.setText("Paste a YouTube URL first.")
            return
        if not folder:
            self.status_label.setText("Choose a save folder first.")
            return
        if not ffmpeg_exists():
            ErrorDialog("FFmpeg Missing", "FFmpeg could not be found on this Mac.", self).exec()
            return

        self.progress_bar.setValue(0)
        self.percent_label.setText("0%")
        self.speed_label.setText("Speed  —")
        self.eta_label.setText("ETA  —")
        self.status_label.setText("Starting…")
        self.status_pill.set_state("working", "Downloading")
        self.download_button.setEnabled(False)
        self.download_button.setText("Downloading…")
        self.stop_button.setEnabled(True)

        self.worker = DownloadWorker(
            url,
            folder,
            self.format_combo.currentText(),
            self.quality_combo.currentText(),
            self.codec_combo.currentText(),
            self.audio_quality_combo.currentText(),
        )
        self.worker.progress.connect(self.update_progress)
        self.worker.speed.connect(lambda v: self.speed_label.setText(f"Speed  {v}"))
        self.worker.eta.connect(lambda v: self.eta_label.setText(f"ETA  {v}"))
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(self.download_finished)
        self.worker.cancelled.connect(self.download_cancelled)
        self.worker.error.connect(self.download_error)
        self.worker.start()

    def stop_download(self):
        if self.worker and self.worker.isRunning():
            self.stop_button.setEnabled(False)
            self.status_pill.set_state("working", "Stopping")
            self.worker.cancel()

    def update_progress(self, value):
        if self.progress_anim is not None:
            self.progress_anim.stop()

        animation = QPropertyAnimation(
            self.progress_bar, b"value", self
        )
        animation.setDuration(180)
        animation.setStartValue(self.progress_bar.value())
        animation.setEndValue(value)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        self.progress_anim = animation
        animation.start()
        self.percent_label.setText(f"{value}%")

    def download_finished(self):
        self.progress_bar.setValue(100)
        self.percent_label.setText("100%")
        self.status_label.setText("Download complete")
        self.status_pill.set_state("success", "Done")
        self.speed_label.setText("Speed  Done")
        self.eta_label.setText("ETA  00:00")
        self.download_button.setEnabled(True)
        self.download_button.setText("Download")
        self.stop_button.setEnabled(False)

    def download_cancelled(self):
        self.status_label.setText("Download stopped")
        self.status_pill.set_state("idle", "Ready")
        self.speed_label.setText("Speed  —")
        self.eta_label.setText("ETA  —")
        self.download_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def download_error(self, message):
        self.status_label.setText("Download failed")
        self.status_pill.set_state("error", "Error")
        self.download_button.setEnabled(True)
        self.download_button.setText("Download")
        self.stop_button.setEnabled(False)
        ErrorDialog("Download Error", message, self).exec()


# ============================================================
# CONVERTER
# ============================================================

class ConverterTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.progress_anim = None
        self._visibility_anims = {}
        self.auto_output_name = True
        self.build_ui()

    def field(self, label):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        text = QLabel(label)
        text.setObjectName("fieldLabel")
        layout.addWidget(text)

        return box, layout

    def build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer.addWidget(scroll)

        page = QWidget()
        page.setObjectName("workspacePage")
        root = QVBoxLayout(page)
        root.setContentsMargins(46, 38, 46, 44)
        root.setSpacing(18)
        scroll.setWidget(page)

        eyebrow = QLabel("FFMPEG")
        eyebrow.setObjectName("eyebrow")
        root.addWidget(eyebrow)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("Converter")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Convert, compress, resize, or extract audio without leaving the workspace.")
        subtitle.setObjectName("pageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status_pill = StatusPill()
        header.addWidget(self.status_pill, 0, Qt.AlignTop)
        root.addLayout(header)

        source = Card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(24, 22, 24, 22)
        source_layout.setSpacing(11)
        top = QHBoxLayout()
        label = QLabel("Source file")
        label.setObjectName("sectionTitle")
        source_hint = QLabel("Drop a file or browse")
        source_hint.setObjectName("cardHint")
        top.addWidget(label)
        top.addStretch()
        top.addWidget(source_hint)
        source_layout.addLayout(top)

        self.drop_zone = DropZone()
        self.drop_zone.clicked.connect(self.choose_input)
        self.drop_zone.file_dropped.connect(self.set_input_file)
        source_layout.addWidget(self.drop_zone)

        file_row = QHBoxLayout()
        file_row.setSpacing(10)
        self.input_file = AppleLineEdit()
        self.input_file.setReadOnly(True)
        self.input_file.setPlaceholderText("No file selected")
        self.input_file.setMinimumHeight(42)
        browse = AppleButton("Browse")
        browse.setObjectName("secondaryButton")
        browse.setFixedWidth(88)
        browse.setMinimumHeight(42)
        browse.clicked.connect(self.choose_input)
        clear = AppleButton("Clear")
        clear.setObjectName("ghostButton")
        clear.setFixedWidth(72)
        clear.setMinimumHeight(42)
        clear.clicked.connect(self.clear_input)
        file_row.addWidget(self.input_file, 1)
        file_row.addWidget(browse)
        file_row.addWidget(clear)
        source_layout.addLayout(file_row)

        self.file_info_label = QLabel("No file selected")
        self.file_info_label.setObjectName("mutedLabel")
        source_layout.addWidget(self.file_info_label)
        root.addWidget(HoverExpandHost(source, 14))

        output = Card()
        out_layout = QVBoxLayout(output)
        out_layout.setContentsMargins(24, 22, 24, 22)
        out_layout.setSpacing(14)
        head = QHBoxLayout()
        label = QLabel("Output")
        label.setObjectName("sectionTitle")
        self.output_hint = QLabel("H.264 • Balanced • Keep resolution")
        self.output_hint.setObjectName("cardHint")
        head.addWidget(label)
        head.addStretch()
        head.addWidget(self.output_hint)
        out_layout.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)

        self.output_format_box, f = self.field("Format")
        self.output_format = AppleComboBox()
        self.output_format.addItems(["MP4", "MKV", "MOV", "WebM", "MP3", "M4A", "WAV", "FLAC", "Opus"])
        f.addWidget(self.output_format)
        grid.addWidget(self.output_format_box, 0, 0)

        self.name_box, n = self.field("File name")
        self.output_name = AppleLineEdit()
        self.output_name.setPlaceholderText("output.mp4")
        self.output_name.setMinimumHeight(42)
        n.addWidget(self.output_name)
        grid.addWidget(self.name_box, 0, 1, 1, 3)

        self.video_codec_box, c = self.field("Video codec")
        self.video_codec = AppleComboBox()
        c.addWidget(self.video_codec)
        self.video_quality_box, q = self.field("Quality")
        self.video_quality = AppleComboBox()
        self.video_quality.addItems(["Source", "High", "Balanced", "Smaller"])
        q.addWidget(self.video_quality)
        self.video_resolution_box, r = self.field("Resolution")
        self.video_resolution = AppleComboBox()
        self.video_resolution.addItems(["Keep", "1080p", "720p", "480p", "360p"])
        r.addWidget(self.video_resolution)
        self.audio_quality_box, a = self.field("Audio bitrate")
        self.audio_quality = AppleComboBox()
        self.audio_quality.addItems(["320 kbps", "256 kbps", "192 kbps", "128 kbps"])
        a.addWidget(self.audio_quality)
        grid.addWidget(self.video_codec_box, 1, 0)
        grid.addWidget(self.video_quality_box, 1, 1)
        grid.addWidget(self.video_resolution_box, 1, 2)
        grid.addWidget(self.audio_quality_box, 1, 3)

        folder_box, folder_layout = self.field("Save to")
        self.output_folder = QLineEdit(os.path.expanduser("~/Downloads"))
        self.output_folder.setMinimumHeight(42)
        folder_layout.addWidget(self.output_folder)
        grid.addWidget(folder_box, 2, 0, 1, 3)
        browse_folder = AppleButton("Choose folder")
        browse_folder.setObjectName("secondaryButton")
        browse_folder.setFixedWidth(130)
        browse_folder.setMinimumHeight(42)
        browse_folder.clicked.connect(self.choose_output_folder)
        grid.addWidget(browse_folder, 2, 3)
        out_layout.addLayout(grid)

        self.preview_label = QLabel("Output  —")
        self.preview_label.setObjectName("pathPreview")
        out_layout.addWidget(self.preview_label)
        root.addWidget(HoverExpandHost(output, 12))

        action = Card()
        action.setObjectName("actionSurface")
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(14, 14, 14, 14)
        action_layout.setSpacing(11)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.convert_button = AppleButton("Convert")
        self.convert_button.setObjectName("primaryButton")
        self.convert_button.setMinimumHeight(52)
        self.stop_button = AppleButton("Stop")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setMinimumHeight(52)
        self.stop_button.setFixedWidth(120)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.convert_button, 1)
        buttons.addWidget(self.stop_button)
        action_layout.addLayout(buttons)
        status = QHBoxLayout()
        self.status_label = QLabel("Ready to convert")
        self.status_label.setObjectName("statusLabel")
        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("percentLabel")
        status.addWidget(self.status_label)
        status.addStretch()
        status.addWidget(self.percent_label)
        action_layout.addLayout(status)
        self.progress_bar = AppleProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        action_layout.addWidget(self.progress_bar)
        meta = QHBoxLayout()
        self.speed_label = QLabel("Speed  —")
        self.eta_label = QLabel("ETA  —")
        self.speed_label.setObjectName("metaLabel")
        self.eta_label.setObjectName("metaLabel")
        meta.addWidget(self.speed_label)
        meta.addStretch()
        meta.addWidget(self.eta_label)
        action_layout.addLayout(meta)
        self.open_folder_button = AppleButton("Open Output Folder")
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.setMinimumHeight(42)
        self.open_folder_button.clicked.connect(self.open_output_folder)
        action_layout.addWidget(self.open_folder_button)
        root.addWidget(HoverExpandHost(action, 10))
        root.addStretch(1)

        self.output_format.currentTextChanged.connect(self.format_changed)
        self.output_name.textChanged.connect(self.output_name_edited)
        self.output_folder.textChanged.connect(self.update_preview)
        self.video_codec.currentTextChanged.connect(self.update_preview)
        self.video_quality.currentTextChanged.connect(self.update_preview)
        self.video_resolution.currentTextChanged.connect(self.update_preview)
        self.audio_quality.currentTextChanged.connect(self.update_preview)
        self.convert_button.clicked.connect(self.start_conversion)
        self.stop_button.clicked.connect(self.stop_conversion)

        self.format_changed(self.output_format.currentText())
        self.status_pill.set_state("idle", "Ready")

    def _animate_visibility(self, widget, show):
        widget.setVisible(show)

    def is_audio_format(self):
        return self.output_format.currentText() in [
            "MP3",
            "M4A",
            "WAV",
            "FLAC",
            "Opus",
        ]

    def format_changed(self, name):
        audio = self.is_audio_format()

        if name == "WebM":
            items = ["VP9", "AV1", "Copy"]
        else:
            items = [
                "H.264",
                "H.265",
                "VP9",
                "AV1",
                "Copy",
            ]

        current = self.video_codec.currentText()

        self.video_codec.blockSignals(True)
        self.video_codec.clear()
        self.video_codec.addItems(items)

        if current in items:
            self.video_codec.setCurrentText(current)
        else:
            self.video_codec.setCurrentText(items[0])

        self.video_codec.blockSignals(False)

        self._animate_visibility(self.video_codec_box, not audio)
        self._animate_visibility(self.video_quality_box, not audio)
        self._animate_visibility(self.video_resolution_box, not audio)
        self._animate_visibility(self.audio_quality_box, audio)

        copy = self.video_codec.currentText() == "Copy"

        self.video_quality.setEnabled(
            not copy and not audio
        )
        self.video_resolution.setEnabled(
            not copy and not audio
        )

        if audio:
            self.output_hint.setText(
                "Audio export • bitrate controls size and quality."
            )
        else:
            self.output_hint.setText(
                f"{self.video_codec.currentText()} • "
                f"{self.video_quality.currentText()} • "
                f"{self.video_resolution.currentText()}"
            )

        self.update_auto_output_extension()
        self.update_preview()

    def update_auto_output_extension(self):
        if not self.auto_output_name:
            return

        ext = self.output_format.currentText().lower()
        path = self.input_file.text().strip()

        if path:
            base = os.path.splitext(
                os.path.basename(path)
            )[0]
            self.output_name.setText(
                f"{base}_converted.{ext}"
            )
        else:
            self.output_name.setText(
                f"output.{ext}"
            )

    def output_name_edited(self, text):
        if self.output_name.hasFocus():
            self.auto_output_name = False

        self.update_preview()

    def set_input_file(self, path):
        self.input_file.setText(path)
        self.on_input_changed()

    def on_input_changed(self):
        path = self.input_file.text().strip()

        if not path:
            self.file_info_label.setText(
                "No file selected"
            )
            self.update_preview()
            return

        if not os.path.isfile(path):
            self.file_info_label.setText(
                "File not found"
            )
            self.update_preview()
            return

        ext = (
            os.path.splitext(path)[1]
            .upper()
            .lstrip(".")
            or "FILE"
        )

        self.file_info_label.setText(
            f"{ext}  •  {format_bytes(os.path.getsize(path))}"
        )

        self.auto_output_name = True
        self.update_auto_output_extension()
        self.update_preview()

        self.status_label.setText(
            "File loaded — choose your output settings"
        )

    def choose_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Media File",
            os.path.expanduser("~/Downloads"),
            "Media Files (*.mp4 *.mkv *.mov *.webm *.avi *.mp3 *.m4a *.wav *.flac *.opus);;All Files (*)"
        )

        if path:
            self.set_input_file(path)

    def clear_input(self):
        self.input_file.clear()
        self.auto_output_name = True

        self.file_info_label.setText(
            "No file selected"
        )

        self.status_label.setText(
            "Ready to convert"
        )

        self.update_auto_output_extension()
        self.update_preview()

    def choose_output_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose Output Folder"
        )

        if folder:
            self.output_folder.setText(folder)

    def get_output_path(self):
        folder = self.output_folder.text().strip()
        name = self.output_name.text().strip()

        if folder and name:
            return os.path.join(
                folder,
                name
            )

        return ""

    def update_preview(self):
        output = self.get_output_path()

        if output:
            self.preview_label.setText(
                f"Output  {output}"
            )
        else:
            self.preview_label.setText(
                "Output  —"
            )

    def set_controls_enabled(self, enabled):
        for widget in [
            self.output_format,
            self.output_name,
            self.output_folder,
            self.video_codec,
            self.video_quality,
            self.video_resolution,
            self.audio_quality,
        ]:
            widget.setEnabled(enabled)

        self.drop_zone.setEnabled(enabled)
        self.input_file.setEnabled(enabled)

    def start_conversion(self):
        input_file = self.input_file.text().strip()
        output_file = self.get_output_path()

        if not input_file or not os.path.isfile(input_file):
            self.status_label.setText(
                "Choose a valid source file first."
            )
            return

        if not output_file:
            self.status_label.setText(
                "Choose an output filename and folder."
            )
            return

        if not ffmpeg_exists():
            AppMessageDialog.critical(
                "FFmpeg Missing",
                "FFmpeg could not be found on this Mac.",
                self
            )
            return

        if os.path.abspath(input_file) == os.path.abspath(output_file):
            AppMessageDialog.warning(
                "Same File",
                "The input and output files cannot be the same.",
                self
            )
            return

        if os.path.exists(output_file):
            answer = AppMessageDialog.question(
                "Replace Existing File?",
                "That output file already exists.\n\nDo you want to replace it?",
                self,
                yes_text="Replace",
                no_text="Cancel",
            )

            if not answer:
                return

        os.makedirs(
            os.path.dirname(output_file),
            exist_ok=True
        )

        self.progress_bar.setValue(0)
        self.percent_label.setText("0%")
        self.status_label.setText("Starting…")
        self.speed_label.setText("Speed  —")
        self.eta_label.setText("ETA  —")

        self.status_pill.set_state(
            "working",
            "Converting"
        )

        self.convert_button.setEnabled(False)
        self.convert_button.setText("Converting…")
        self.stop_button.setEnabled(True)
        self.open_folder_button.setEnabled(False)

        self.set_controls_enabled(False)

        self.worker = ConverterWorker(
            input_file,
            output_file,
            self.output_format.currentText(),
            (
                self.video_codec.currentText()
                if not self.is_audio_format()
                else "Copy"
            ),
            self.video_quality.currentText(),
            self.video_resolution.currentText(),
            self.audio_quality.currentText(),
        )

        self.worker.progress.connect(
            self.update_progress
        )
        self.worker.status.connect(
            self.status_label.setText
        )
        self.worker.speed.connect(
            lambda v: self.speed_label.setText(
                f"Speed  {v}"
            )
        )
        self.worker.eta.connect(
            lambda v: self.eta_label.setText(
                f"ETA  {v}"
            )
        )
        self.worker.finished.connect(
            self.conversion_finished
        )
        self.worker.cancelled.connect(
            self.conversion_cancelled
        )
        self.worker.error.connect(
            self.conversion_error
        )

        self.worker.start()

    def stop_conversion(self):
        if self.worker and self.worker.isRunning():
            self.stop_button.setEnabled(False)

            self.status_pill.set_state(
                "working",
                "Stopping"
            )

            self.worker.cancel()

    def update_progress(self, value):
        if self.progress_anim is not None:
            self.progress_anim.stop()

        animation = QPropertyAnimation(
            self.progress_bar, b"value", self
        )
        animation.setDuration(180)
        animation.setStartValue(self.progress_bar.value())
        animation.setEndValue(value)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        self.progress_anim = animation
        animation.start()
        self.percent_label.setText(
            f"{value}%"
        )

    def conversion_finished(self, output_file):
        self.progress_bar.setValue(100)
        self.percent_label.setText("100%")

        self.status_label.setText(
            "Conversion complete"
        )

        self.status_pill.set_state(
            "success",
            "Done"
        )

        self.speed_label.setText(
            "Speed  Done"
        )
        self.eta_label.setText(
            "ETA  00:00"
        )

        self.convert_button.setEnabled(True)
        self.convert_button.setText("Convert")
        self.stop_button.setEnabled(False)

        self.set_controls_enabled(True)
        self.open_folder_button.setEnabled(True)

        AppMessageDialog.information(
            "Conversion Complete",
            f"Your file is ready.\n\n{output_file}",
            self
        )

    def conversion_cancelled(self):
        self.status_label.setText(
            "Conversion stopped"
        )

        self.status_pill.set_state(
            "idle",
            "Ready"
        )

        self.speed_label.setText(
            "Speed  —"
        )
        self.eta_label.setText(
            "ETA  —"
        )

        self.convert_button.setEnabled(True)
        self.convert_button.setText("Convert")
        self.stop_button.setEnabled(False)
        self.set_controls_enabled(True)

    def conversion_error(self, message):
        self.status_label.setText(
            "Conversion failed"
        )

        self.status_pill.set_state(
            "error",
            "Error"
        )

        self.convert_button.setEnabled(True)
        self.convert_button.setText("Convert")
        self.stop_button.setEnabled(False)

        self.set_controls_enabled(True)

        ErrorDialog("Conversion Error", message, self).exec()

    def open_output_folder(self):
        folder = self.output_folder.text().strip()

        if not os.path.isdir(folder):
            return

        if sys.platform == "darwin":
            subprocess.Popen(["open", folder])

        elif sys.platform.startswith("win"):
            os.startfile(folder)

        else:
            subprocess.Popen(["xdg-open", folder])


# ============================================================
# ANIMATED PAGE HOST
# ============================================================

class AnimatedPageHost(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pageHost")
        self.pages = []
        self.current_index = 0
        self.anim_group = None
        self.is_animating = False

    def add_page(self, page):
        page.setParent(self)
        page.show() if not self.pages else page.hide()
        page.setGeometry(self.rect())
        self.pages.append(page)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.is_animating:
            for page in self.pages:
                page.setGeometry(self.rect())

    def _reset_pages(self):
        for i, page in enumerate(self.pages):
            page.setGeometry(self.rect())
            page.setVisible(i == self.current_index)
        self.is_animating = False

    def switch_to(self, index):
        if not (0 <= index < len(self.pages)):
            return
        if index == self.current_index and not self.is_animating:
            return

        if self.anim_group:
            self.anim_group.stop()
            self.anim_group = None
            self._reset_pages()

        old_page = self.pages[self.current_index]
        new_page = self.pages[index]
        direction = 1 if index > self.current_index else -1
        width = max(1, self.width())

        self.is_animating = True
        old_page.show()
        new_page.show()
        old_page.setGeometry(self.rect())
        new_page.setGeometry(self.rect().translated(direction * width, 0))
        new_page.raise_()

        old_anim = QPropertyAnimation(old_page, b"pos", self)
        old_anim.setDuration(300)
        old_anim.setStartValue(QPoint(0, 0))
        old_anim.setEndValue(QPoint(-direction * width, 0))
        old_anim.setEasingCurve(QEasingCurve.InOutCubic)

        new_anim = QPropertyAnimation(new_page, b"pos", self)
        new_anim.setDuration(340)
        new_anim.setStartValue(QPoint(direction * width, 0))
        new_anim.setEndValue(QPoint(0, 0))
        new_anim.setEasingCurve(QEasingCurve.OutCubic)

        group = QParallelAnimationGroup(self)
        group.addAnimation(old_anim)
        group.addAnimation(new_anim)

        def finished():
            old_page.hide()
            old_page.setGeometry(self.rect())
            new_page.setGeometry(self.rect())
            new_page.show()
            self.current_index = index
            self.is_animating = False
            self.anim_group = None

        group.finished.connect(finished)
        self.anim_group = group
        group.start()


class AppMessageDialog(QDialog):
    """Unified dark, readable application dialog for all user-facing messages."""

    def __init__(
        self,
        title,
        message,
        parent=None,
        kind="info",
        buttons=None,
        details=None,
    ):
        super().__init__(parent)

        self.result_value = False
        self._drag_origin = None
        self.details_text = details

        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumWidth(470)
        self.resize(540, 260 if not details else 440)

        if buttons is None:
            buttons = [("OK", True, "primary")]

        icon_chars = {
            "info": "i",
            "warning": "!",
            "error": "!",
            "question": "?",
        }
        icon = icon_chars.get(kind, "i")

        colors = {
            "info": ("#0A84FF", "#17304A"),
            "warning": ("#FF9F0A", "#4A3416"),
            "error": ("#FF453A", "#4A1D1A"),
            "question": ("#0A84FF", "#17304A"),
        }
        accent, icon_bg = colors.get(kind, colors["info"])

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(0)

        card = QFrame()
        card.setObjectName("dialogCard")
        card.setStyleSheet(f"""
            QFrame#dialogCard {{
                background: #1A1B1F;
                border: 1px solid #34363D;
                border-radius: 18px;
            }}
            QLabel#dialogTitle {{
                color: #F5F5F7;
                font-size: 15px;
                font-weight: 600;
            }}
            QLabel#dialogMessage {{
                color: #B9BBC2;
                font-size: 13px;
                line-height: 1.4;
            }}
            QLabel#dialogIcon {{
                background: {icon_bg};
                color: {accent};
                border: 1px solid {accent};
                border-radius: 12px;
                font-size: 15px;
                font-weight: 700;
            }}
            QFrame#dialogBar {{
                background: #15161A;
                border-bottom: 1px solid #2A2C31;
            }}
            QLabel#trafficRed, QLabel#trafficYellow, QLabel#trafficGreen {{
                border-radius: 6px;
                min-width: 12px;
                max-width: 12px;
                min-height: 12px;
                max-height: 12px;
            }}
            QLabel#trafficRed {{ background: #FF5F57; }}
            QLabel#trafficYellow {{ background: #FEBC2E; }}
            QLabel#trafficGreen {{ background: #28C840; }}
            QTextEdit#details {{
                background: #111216;
                color: #B8BAC2;
                border: 1px solid #2D3037;
                border-radius: 11px;
                padding: 10px;
                font-family: Menlo, Monaco, Consolas, monospace;
                font-size: 11px;
            }}
            QPushButton#dialogButton {{
                background: #25272C;
                color: #F1F2F4;
                border: 1px solid #3B3E46;
                border-radius: 10px;
                padding: 0 16px;
                min-height: 36px;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton#dialogButton:hover {{
                background: #2D3036;
                border-color: #50535C;
            }}
            QPushButton#dialogPrimary {{
                background: {accent};
                color: #FFFFFF;
                border: 1px solid {accent};
            }}
            QPushButton#dialogPrimary:hover {{
                background: {accent};
            }}
            QPushButton#dialogButton:pressed, QPushButton#dialogPrimary:pressed {{
                padding-top: 2px;
            }}
        """)
        root.addWidget(card)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("dialogBar")
        bar.setFixedHeight(42)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(16, 0, 16, 0)
        bar_layout.setSpacing(7)
        for obj in ("trafficRed", "trafficYellow", "trafficGreen"):
            dot = QLabel()
            dot.setObjectName(obj)
            bar_layout.addWidget(dot)
        bar_layout.addStretch()
        card_layout.addWidget(bar)
        bar.mousePressEvent = self._bar_press
        bar.mouseMoveEvent = self._bar_move

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 22, 24, 22)
        body_layout.setSpacing(15)
        card_layout.addWidget(body)

        heading_row = QHBoxLayout()
        heading_row.setSpacing(13)

        icon_label = QLabel(icon)
        icon_label.setObjectName("dialogIcon")
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setFixedSize(40, 40)
        heading_row.addWidget(icon_label)

        heading = QLabel(title)
        heading.setObjectName("dialogTitle")
        heading.setWordWrap(True)
        heading_row.addWidget(heading, 1)
        body_layout.addLayout(heading_row)

        msg = QLabel(message)
        msg.setObjectName("dialogMessage")
        msg.setWordWrap(True)
        msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body_layout.addWidget(msg)

        if details:
            details_box = QTextEdit()
            details_box.setObjectName("details")
            details_box.setReadOnly(True)
            details_box.setPlainText(details)
            body_layout.addWidget(details_box, 1)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(9)
        buttons_row.addStretch()

        for text, accepted, role in buttons:
            button = QPushButton(text)
            button.setObjectName("dialogPrimary" if role == "primary" else "dialogButton")
            button.setDefault(accepted and role == "primary")
            button.clicked.connect(lambda checked=False, a=accepted: self._finish(a))
            buttons_row.addWidget(button)

        body_layout.addLayout(buttons_row)

        self.adjustSize()

    def _finish(self, accepted):
        self.result_value = accepted
        self.done(QDialog.Accepted if accepted else QDialog.Rejected)

    def _bar_press(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _bar_move(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()

    @classmethod
    def information(cls, title, message, parent=None):
        dlg = cls(title, message, parent, "info")
        return dlg.exec()

    @classmethod
    def warning(cls, title, message, parent=None):
        dlg = cls(title, message, parent, "warning")
        return dlg.exec()

    @classmethod
    def critical(cls, title, message, parent=None, details=None):
        dlg = cls(title, message, parent, "error", details=details)
        return dlg.exec()

    @classmethod
    def question(cls, title, message, parent=None, yes_text="Yes", no_text="No"):
        dlg = cls(
            title,
            message,
            parent,
            "question",
            buttons=[
                (no_text, False, "secondary"),
                (yes_text, True, "primary"),
            ],
        )
        return dlg.exec() == QDialog.Accepted


class ErrorDialog(AppMessageDialog):
    """Compatibility wrapper for existing error-dialog call sites."""
    def __init__(self, title, message, parent=None):
        super().__init__(
            title,
            "Something went wrong. See the technical details below.",
            parent,
            kind="error",
            details=message,
        )


# ============================================================
# MAIN WINDOW
# ============================================================

class MediaToolkitWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Toolkit")
        self.setMinimumSize(1060, 720)
        self.resize(1240, 840)
        self.setAttribute(Qt.WA_StyledBackground, True)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Sidebar
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(226)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 18, 18, 20)
        side.setSpacing(0)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        icon = QLabel("M")
        icon.setObjectName("brandIcon")
        brand.addWidget(icon)
        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        name = QLabel("Media Toolkit")
        name.setObjectName("brandName")
        version = QLabel("Editor workspace")
        version.setObjectName("brandVersion")
        text_box.addWidget(name)
        text_box.addWidget(version)
        brand.addLayout(text_box)
        side.addLayout(brand)
        side.addSpacing(38)

        section = QLabel("Workspace")
        section.setObjectName("sidebarLabel")
        side.addWidget(section)
        side.addSpacing(7)

        self.nav = SidebarNav()
        self.nav.page_requested.connect(self.switch_page)
        side.addWidget(self.nav)
        side.addStretch(1)

        footer = QVBoxLayout()
        footer.setSpacing(2)
        f1 = QLabel("Local utility")
        f1.setObjectName("sidebarFooterStrong")
        f2 = QLabel("FFmpeg  •  yt-dlp")
        f2.setObjectName("sidebarFooter")
        footer.addWidget(f1)
        footer.addWidget(f2)
        side.addLayout(footer)
        root.addWidget(sidebar)

        # Main surface
        content = QFrame()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(30, 13, 32, 13)
        top_layout.addStretch()
        self.top_status = QLabel("●  Local  ·  Ready")
        self.top_status.setObjectName("topStatus")
        top_layout.addWidget(self.top_status)
        content_layout.addWidget(topbar)

        self.pages = AnimatedPageHost()
        self.downloader_tab = DownloaderTab()
        self.converter_tab = ConverterTab()
        self.pages.add_page(self.downloader_tab)
        self.pages.add_page(self.converter_tab)
        content_layout.addWidget(self.pages, 1)
        root.addWidget(content, 1)

        QTimer.singleShot(0, self._prepare_window)

    def _prepare_window(self):
        self.nav._place_indicator()
        self.pages._reset_pages()

    def switch_page(self, index):
        if index not in (0, 1):
            return
        self.nav.downloader.setChecked(index == 0)
        self.nav.converter.setChecked(index == 1)
        self.nav.set_index(index)
        self.pages.switch_to(index)


# ============================================================
# STYLES
# ============================================================

STYLE = """
* { outline: none; }
QWidget { color: #F5F5F7; font-size: 13px; }
QToolTip { background: #232429; color: #F5F5F7; border: 1px solid #3B3D43; border-radius: 10px; padding: 7px 10px; }
#content, #pageHost, #pageScroll, #workspacePage { background: #0D0E10; border: 0; }
#sidebar { background: #111215; border-right: 1px solid #24262B; }
#topbar { background: #111215; border-bottom: 1px solid #23252A; }
#brandIcon { background: #F5F5F7; color: #17181A; border-radius: 11px; min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px; font-size: 17px; font-weight: 600; qproperty-alignment: AlignCenter; }
#brandName { color: #F5F5F7; font-size: 13px; font-weight: 600; }
#brandVersion { color: #80828A; font-size: 10px; }
#sidebarLabel { color: #70727A; font-size: 10px; font-weight: 600; margin: 0 5px; }
#sidebarFooter { color: #63656C; font-size: 10px; padding: 0 4px; }
#topStatus { color: #8F929A; font-size: 10px; font-weight: 500; }
#sidebarIndicator { background: #22242A; border: 1px solid #32353C; border-radius: 11px; }
#eyebrow { color: #777A82; font-size: 10px; font-weight: 600; letter-spacing: 1.1px; }
#pageTitle { color: #F5F5F7; font-size: 33px; font-weight: 600; letter-spacing: -0.7px; }
#pageSubtitle { color: #90939B; font-size: 13px; font-weight: 400; }
#sectionTitle { color: #ECEDEF; font-size: 13px; font-weight: 600; }
#cardHint { color: #777A82; font-size: 11px; }
#fieldLabel { color: #7B7E86; font-size: 10px; font-weight: 500; }
#hint, #inlineHint { color: #777A82; font-size: 10px; }
QPushButton {
    background: #191A1E;
    color: #F0F0F2;
    border: 1px solid #34373E;
    border-radius: 13px;
    padding: 0 14px;
    min-height: 40px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover { background: #202126; border-color: #494C54; }
QPushButton:pressed { background: #15161A; border-color: #3D4047; }
QPushButton:disabled { background: #15161A; color: #60636B; border-color: #27292E; }
QPushButton#primaryButton { background: #0A84FF; color: #FFFFFF; border-color: #4BA8FF; font-weight: 600; }
QPushButton#primaryButton:hover { background: #168EFF; border-color: #78BEFF; }
QPushButton#primaryButton:pressed { background: #0879E6; border-color: #3298F5; }
QPushButton#secondaryButton { background: #1A1B1F; border-color: #373A41; }
QPushButton#secondaryButton:hover { background: #23252A; border-color: #4C4F57; }
QPushButton#ghostButton { background: transparent; border-color: transparent; color: #AEB1B9; }
QPushButton#ghostButton:hover { background: #1B1D21; border-color: #35383F; }
QPushButton#dangerButton { background: #19181C; color: #B8B4BA; border-color: #303238; }
QPushButton#dangerButton:hover { background: #232025; border-color: #4B4148; }
QPushButton#sidebarButton { background: transparent; border: 0; border-radius: 12px; color: #8E9098; text-align: left; padding-left: 12px; }
QPushButton#sidebarButton:hover { background: rgba(255,255,255,0.04); color: #F5F5F7; }
QPushButton#sidebarButton:checked { color: #F5F5F7; }
QPushButton QLabel#buttonContent { background: transparent; color: #F0F0F2; border: 0; }
QPushButton#primaryButton QLabel#buttonContent { color: #FFFFFF; font-weight: 600; }
QPushButton:disabled QLabel#buttonContent { color: #60636B; }
QPushButton#sidebarButton QLabel#buttonContent { color: #8E9098; font-weight: 500; }
QPushButton#sidebarButton:hover QLabel#buttonContent, QPushButton#sidebarButton:checked QLabel#buttonContent { color: #F5F5F7; }
#dropIcon { background: #24252A; color: #F4F4F5; border: 1px solid #33363D; border-radius: 13px; font-size: 24px; font-weight: 500; }

QLineEdit { background: #16171A; color: #F5F5F7; border: 1px solid #2C2F35; border-radius: 13px; padding: 0 13px; min-height: 40px; selection-background-color: #0A84FF; selection-color: #FFFFFF; }
QLineEdit:hover { background: #1A1B1F; border-color: #3A3D44; }
QLineEdit:focus { background: #1B1C20; border-color: #4A9BFF; }
QLineEdit:disabled { color: #60636B; background: #131417; border-color: #25272C; }
#pathPreview { background: #121417; border: 1px solid #25282D; border-radius: 10px; color: #777A82; padding: 9px 12px; font-size: 10px; }
#statusLabel { color: #E7E8EC; font-size: 12px; font-weight: 500; }
#percentLabel { color: #B4B6BD; font-size: 11px; font-weight: 600; }
#mutedLabel { color: #72757D; font-size: 10px; }
#metaLabel { color: #777A82; font-size: 10px; }
#dropZone { background: transparent; border: 0; }
#dropTitle { color: #F2F3F5; font-size: 15px; font-weight: 600; }
#dropSubtitle { color: #858890; font-size: 11px; }
#dropTypes { color: #666970; font-size: 9px; }
#actionSurface { background: #17181C; border: 1px solid #2B2E34; border-radius: 19px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 5px 1px 5px 0; }
QScrollBar::handle:vertical { background: #36383E; border-radius: 4px; min-height: 34px; }
QScrollBar::handle:vertical:hover { background: #4A4C52; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
#appleCombo { background: transparent; border: 0; }
#sidebarButton { background: transparent; border: 0; }
"""

# ============================================================
# APPLICATION ENTRY POINT
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Media Toolkit")
    app.setApplicationDisplayName("Media Toolkit")

    # Prefer Apple's installed system UI font on macOS; never bundle it.
    families = set(QFontDatabase.families())
    preferred = ["SF Pro Text", ".SF NS Text", ".AppleSystemUIFont"] if sys.platform == "darwin" else []
    chosen = next((name for name in preferred if name in families), None)
    system_font = QFont(chosen) if chosen else QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    system_font.setPointSizeF(13.0)
    system_font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
    app.setFont(system_font)

    app.setStyle("Fusion")
    font_family = chosen or system_font.family()
    app.setStyleSheet(STYLE + f'\nQWidget {{ font-family: "{font_family}"; }}\n')

    window = MediaToolkitWindow()
    window.show()

    sys.exit(app.exec())
