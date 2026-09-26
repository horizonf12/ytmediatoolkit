import sys
import os
import time
import shutil
import tempfile
import subprocess
import threading

import yt_dlp

from PySide6.QtCore import QThread, Signal, QUrl, Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
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
    QTabWidget,
    QGroupBox,
    QFormLayout,
    QMessageBox,
    QSizePolicy,
    QFrame,
    QStackedWidget,
    QGridLayout,
    QScrollArea,
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
    """Return a bundled FFmpeg binary when packaged, otherwise use PATH."""
    if getattr(sys, "frozen", False):
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidate = os.path.join(base_dir, name)
        if os.path.isfile(candidate):
            return candidate

    return shutil.which(name) or name


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
            "windowsfilenames": True,
            "noplaylist": True,
        }

        self.status.emit("Downloading audio...")

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                self.url,
                download=True
            )

            filename = ydl.prepare_filename(info)
            return filename

    def build_ffmpeg_command(self, input_file, output_file):
        if self.format_choice == "MP3":
            bitrate = self.audio_quality.replace(" kbps", "")

            return [
                ffmpeg_path(),
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
            self.status.emit("Download complete!")
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
# POLISHED UI
# ============================================================

class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")


class StatusPill(QLabel):
    def __init__(self, text="READY", parent=None):
        super().__init__(text, parent)
        self.setObjectName("statusPill")
        self.setAlignment(Qt.AlignCenter)

    def set_state(self, state, text):
        self.setText(text)
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class SidebarButton(QPushButton):
    def __init__(self, icon, text, parent=None):
        super().__init__(parent)
        self.setText(f"{icon}   {text}")
        self.setObjectName("sidebarButton")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(50)


class DropZone(QFrame):
    file_dropped = Signal(str)
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("dropZone")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(6)
        layout.setContentsMargins(20, 24, 20, 24)

        icon = QLabel("＋")
        icon.setObjectName("dropIcon")
        icon.setAlignment(Qt.AlignCenter)

        title = QLabel("Drop a media file here")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignCenter)

        subtitle = QLabel("or click to browse  •  MP4, MKV, MOV, WebM, MP3, WAV and more")
        subtitle.setObjectName("dropSubtitle")
        subtitle.setAlignment(Qt.AlignCenter)

        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(subtitle)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            self.setProperty("dragging", True)
            self.style().unpolish(self)
            self.style().polish(self)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        event.accept()

    def dropEvent(self, event):
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.file_dropped.emit(url.toLocalFile())
                event.acceptProposedAction()
                return
        event.ignore()


# ============================================================
# DOWNLOADER
# ============================================================

class DownloaderTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.build_ui()

    def field(self, label):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        text = QLabel(label.upper())
        text.setObjectName("fieldLabel")
        layout.addWidget(text)
        return box, layout

    def build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(34, 26, 34, 32)
        root.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title = QLabel("YouTube Downloader")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Download video or extract audio with a few clean controls.")
        subtitle.setObjectName("pageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status_pill = StatusPill()
        header.addWidget(self.status_pill, 0, Qt.AlignTop)
        root.addLayout(header)

        source = Card()
        layout = QVBoxLayout(source)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(9)
        label = QLabel("SOURCE")
        label.setObjectName("eyebrow")
        layout.addWidget(label)

        row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste a YouTube URL…")
        self.url_input.setClearButtonEnabled(True)
        paste = QPushButton("Paste")
        paste.setObjectName("secondaryButton")
        paste.clicked.connect(self.paste_url)
        row.addWidget(self.url_input, 1)
        row.addWidget(paste)
        layout.addLayout(row)
        root.addWidget(source)

        settings = Card()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(20, 18, 20, 20)
        settings_layout.setSpacing(13)
        top = QHBoxLayout()
        e = QLabel("OUTPUT SETTINGS")
        e.setObjectName("eyebrow")
        self.hint = QLabel("H.264 is the safest default for editors.")
        self.hint.setObjectName("hint")
        top.addWidget(e)
        top.addStretch()
        top.addWidget(self.hint)
        settings_layout.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.format_box, f = self.field("Format")
        self.format_combo = QComboBox()
        self.format_combo.addItems(["MP4", "MKV", "WebM", "MP3", "M4A", "WAV", "FLAC", "Opus"])
        f.addWidget(self.format_combo)

        self.quality_box, q = self.field("Video quality")
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Best Available", "1080p", "720p", "480p", "360p"])
        q.addWidget(self.quality_combo)

        self.codec_box, c = self.field("Video codec")
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["H.264", "VP9", "AV1", "Any"])
        self.codec_combo.setCurrentText("H.264")
        c.addWidget(self.codec_combo)

        self.audio_box, a = self.field("Audio quality")
        self.audio_quality_combo = QComboBox()
        self.audio_quality_combo.addItems(["320 kbps", "256 kbps", "192 kbps", "128 kbps"])
        a.addWidget(self.audio_quality_combo)

        row.addWidget(self.format_box, 1)
        row.addWidget(self.quality_box, 1)
        row.addWidget(self.codec_box, 1)
        row.addWidget(self.audio_box, 1)
        settings_layout.addLayout(row)
        root.addWidget(settings)

        dest = Card()
        dest_layout = QVBoxLayout(dest)
        dest_layout.setContentsMargins(20, 18, 20, 20)
        dest_layout.setSpacing(8)
        d = QLabel("SAVE TO")
        d.setObjectName("eyebrow")
        dest_layout.addWidget(d)
        row = QHBoxLayout()
        self.folder_input = QLineEdit(os.path.expanduser("~/Downloads"))
        browse = QPushButton("Choose folder")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self.choose_folder)
        row.addWidget(self.folder_input, 1)
        row.addWidget(browse)
        dest_layout.addLayout(row)
        root.addWidget(dest)

        action = Card()
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(20, 18, 20, 20)
        action_layout.setSpacing(12)
        buttons = QHBoxLayout()
        self.download_button = QPushButton("↓  DOWNLOAD")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setMinimumHeight(52)
        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setMinimumHeight(52)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.download_button, 3)
        buttons.addWidget(self.stop_button, 1)
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

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        action_layout.addWidget(self.progress_bar)

        meta = QHBoxLayout()
        self.speed_label = QLabel("Speed  —")
        self.eta_label = QLabel("ETA  —")
        meta.addWidget(self.speed_label)
        meta.addStretch()
        meta.addWidget(self.eta_label)
        action_layout.addLayout(meta)
        root.addWidget(action)
        root.addStretch()

        self.audio_box.setVisible(False)
        self.format_combo.currentTextChanged.connect(self.format_changed)
        self.download_button.clicked.connect(self.start_download)
        self.stop_button.clicked.connect(self.stop_download)
        self.status_pill.set_state("idle", "READY")

    def paste_url(self):
        text = QApplication.clipboard().text().strip()
        if text:
            self.url_input.setText(text)
            self.status_label.setText("URL pasted — ready")

    def format_changed(self, name):
        audio = name in ["MP3", "M4A", "WAV", "FLAC", "Opus"]
        self.quality_box.setVisible(not audio)
        self.codec_box.setVisible(not audio)
        self.audio_box.setVisible(audio)
        self.hint.setText("Audio extraction • choose bitrate below." if audio else "H.264 is the safest default for editors.")

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
            QMessageBox.critical(self, "FFmpeg Missing", "FFmpeg could not be found on this Mac.")
            return

        self.progress_bar.setValue(0)
        self.percent_label.setText("0%")
        self.speed_label.setText("Speed  —")
        self.eta_label.setText("ETA  —")
        self.status_label.setText("Starting…")
        self.status_pill.set_state("working", "DOWNLOADING")
        self.download_button.setEnabled(False)
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
            self.status_pill.set_state("working", "STOPPING")
            self.worker.cancel()

    def update_progress(self, value):
        self.progress_bar.setValue(value)
        self.percent_label.setText(f"{value}%")

    def download_finished(self):
        self.progress_bar.setValue(100)
        self.percent_label.setText("100%")
        self.status_label.setText("Download complete")
        self.status_pill.set_state("success", "DONE")
        self.speed_label.setText("Speed  Done")
        self.eta_label.setText("ETA  00:00")
        self.download_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def download_cancelled(self):
        self.status_label.setText("Download stopped")
        self.status_pill.set_state("idle", "READY")
        self.speed_label.setText("Speed  —")
        self.eta_label.setText("ETA  —")
        self.download_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def download_error(self, message):
        self.status_label.setText("Download failed")
        self.status_pill.set_state("error", "ERROR")
        self.download_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        QMessageBox.critical(self, "Download Error", message)


# ============================================================
# CONVERTER
# ============================================================

class ConverterTab(QWidget):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.auto_output_name = True
        self.build_ui()

    def field(self, label):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        text = QLabel(label.upper())
        text.setObjectName("fieldLabel")
        layout.addWidget(text)

        return box, layout

    def build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # The old converter was a fixed vertical stack. On macOS that could
        # compress the cards enough for sibling widgets to visually collide.
        # Put the workspace inside a real scroll area so it remains correct
        # on smaller windows too.
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        page = QWidget()
        page.setObjectName("converterPage")
        root = QVBoxLayout(page)
        root.setContentsMargins(34, 28, 34, 34)
        root.setSpacing(14)

        scroll.setWidget(page)
        outer.addWidget(scroll)

        # --------------------------------------------------------
        # HEADER
        # --------------------------------------------------------

        header = QHBoxLayout()
        header.setSpacing(18)

        title_box = QVBoxLayout()
        title_box.setSpacing(4)

        title = QLabel("Media Converter")
        title.setObjectName("pageTitle")

        subtitle = QLabel(
            "Convert, compress, resize or extract audio with FFmpeg."
        )
        subtitle.setObjectName("pageSubtitle")

        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        header.addLayout(title_box)
        header.addStretch()

        self.status_pill = StatusPill()
        header.addWidget(self.status_pill, 0, Qt.AlignTop)

        root.addLayout(header)

        # --------------------------------------------------------
        # SOURCE CARD
        # --------------------------------------------------------

        source = Card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(18, 16, 18, 16)
        source_layout.setSpacing(9)

        label = QLabel("SOURCE FILE")
        label.setObjectName("eyebrow")
        source_layout.addWidget(label)

        self.drop_zone = DropZone()
        self.drop_zone.setMinimumHeight(92)
        self.drop_zone.setMaximumHeight(106)
        self.drop_zone.clicked.connect(self.choose_input)
        self.drop_zone.file_dropped.connect(self.set_input_file)
        source_layout.addWidget(self.drop_zone)

        file_row = QHBoxLayout()
        file_row.setSpacing(8)

        self.input_file = QLineEdit()
        self.input_file.setReadOnly(True)
        self.input_file.setPlaceholderText("No file selected")

        browse = QPushButton("Browse")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self.choose_input)

        clear = QPushButton("Clear")
        clear.setObjectName("ghostButton")
        clear.clicked.connect(self.clear_input)

        file_row.addWidget(self.input_file, 1)
        file_row.addWidget(browse)
        file_row.addWidget(clear)

        source_layout.addLayout(file_row)

        self.file_info_label = QLabel("No file selected")
        self.file_info_label.setObjectName("mutedLabel")
        source_layout.addWidget(self.file_info_label)

        root.addWidget(source)

        # --------------------------------------------------------
        # OUTPUT CARD
        # --------------------------------------------------------

        output = Card()
        out_layout = QVBoxLayout(output)
        out_layout.setContentsMargins(18, 16, 18, 16)
        out_layout.setSpacing(10)

        output_header = QHBoxLayout()

        label = QLabel("OUTPUT")
        label.setObjectName("eyebrow")

        self.output_hint = QLabel("H.264 • Balanced • Keep resolution")
        self.output_hint.setObjectName("hint")

        output_header.addWidget(label)
        output_header.addStretch()
        output_header.addWidget(self.output_hint)

        out_layout.addLayout(output_header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)

        # Row 1
        self.output_format_box, f = self.field("Format")
        self.output_format = QComboBox()
        self.output_format.addItems([
            "MP4", "MKV", "MOV", "WebM",
            "MP3", "M4A", "WAV", "FLAC", "Opus"
        ])
        f.addWidget(self.output_format)

        self.name_box, n = self.field("File name")
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("output.mp4")
        n.addWidget(self.output_name)

        grid.addWidget(self.output_format_box, 0, 0)
        grid.addWidget(self.name_box, 0, 1, 1, 3)

        # Row 2
        self.video_codec_box, c = self.field("Video codec")
        self.video_codec = QComboBox()
        c.addWidget(self.video_codec)

        self.video_quality_box, q = self.field("Quality")
        self.video_quality = QComboBox()
        self.video_quality.addItems([
            "Source", "High", "Balanced", "Smaller"
        ])
        q.addWidget(self.video_quality)

        self.video_resolution_box, r = self.field("Resolution")
        self.video_resolution = QComboBox()
        self.video_resolution.addItems([
            "Keep", "1080p", "720p", "480p", "360p"
        ])
        r.addWidget(self.video_resolution)

        self.audio_quality_box, a = self.field("Audio bitrate")
        self.audio_quality = QComboBox()
        self.audio_quality.addItems([
            "320 kbps", "256 kbps", "192 kbps", "128 kbps"
        ])
        a.addWidget(self.audio_quality)

        grid.addWidget(self.video_codec_box, 1, 0)
        grid.addWidget(self.video_quality_box, 1, 1)
        grid.addWidget(self.video_resolution_box, 1, 2)
        grid.addWidget(self.audio_quality_box, 1, 3)

        # Row 3 — destination
        folder_box, folder_layout = self.field("Save to")

        self.output_folder = QLineEdit(
            os.path.expanduser("~/Downloads")
        )
        folder_layout.addWidget(self.output_folder)

        grid.addWidget(folder_box, 2, 0, 1, 3)

        browse_folder = QPushButton("Choose folder")
        browse_folder.setObjectName("secondaryButton")
        browse_folder.clicked.connect(self.choose_output_folder)
        grid.addWidget(browse_folder, 2, 3)

        out_layout.addLayout(grid)

        self.preview_label = QLabel("Output  —")
        self.preview_label.setObjectName("pathPreview")
        self.preview_label.setWordWrap(False)
        out_layout.addWidget(self.preview_label)

        root.addWidget(output)

        # --------------------------------------------------------
        # ACTION CARD
        # --------------------------------------------------------

        action = Card()
        action_layout = QVBoxLayout(action)
        action_layout.setContentsMargins(18, 16, 18, 16)
        action_layout.setSpacing(9)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)

        self.convert_button = QPushButton("⇄  CONVERT")
        self.convert_button.setObjectName("primaryButton")
        self.convert_button.setMinimumHeight(48)

        self.stop_button = QPushButton("STOP")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setMinimumHeight(48)
        self.stop_button.setEnabled(False)

        buttons.addWidget(self.convert_button, 3)
        buttons.addWidget(self.stop_button, 1)

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

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setValue(0)
        action_layout.addWidget(self.progress_bar)

        meta = QHBoxLayout()

        self.speed_label = QLabel("Speed  —")
        self.eta_label = QLabel("ETA  —")

        meta.addWidget(self.speed_label)
        meta.addStretch()
        meta.addWidget(self.eta_label)

        action_layout.addLayout(meta)

        self.open_folder_button = QPushButton("OPEN OUTPUT FOLDER")
        self.open_folder_button.setObjectName("secondaryButton")
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.clicked.connect(
            self.open_output_folder
        )
        action_layout.addWidget(self.open_folder_button)

        root.addWidget(action)

        # Keep a little breathing room without forcing widgets to shrink.
        root.addStretch(1)

        # --------------------------------------------------------
        # SIGNALS
        # --------------------------------------------------------

        self.output_format.currentTextChanged.connect(
            self.format_changed
        )
        self.output_name.textChanged.connect(
            self.output_name_edited
        )
        self.output_folder.textChanged.connect(
            self.update_preview
        )
        self.video_codec.currentTextChanged.connect(
            self.update_preview
        )
        self.video_quality.currentTextChanged.connect(
            self.update_preview
        )
        self.video_resolution.currentTextChanged.connect(
            self.update_preview
        )
        self.audio_quality.currentTextChanged.connect(
            self.update_preview
        )

        self.convert_button.clicked.connect(
            self.start_conversion
        )
        self.stop_button.clicked.connect(
            self.stop_conversion
        )

        self.format_changed(
            self.output_format.currentText()
        )

        self.status_pill.set_state(
            "idle",
            "READY"
        )

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

        self.video_codec_box.setVisible(not audio)
        self.video_quality_box.setVisible(not audio)
        self.video_resolution_box.setVisible(not audio)
        self.audio_quality_box.setVisible(audio)

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
            QMessageBox.critical(
                self,
                "FFmpeg Missing",
                "FFmpeg could not be found on this Mac."
            )
            return

        if os.path.abspath(input_file) == os.path.abspath(output_file):
            QMessageBox.warning(
                self,
                "Same File",
                "The input and output files cannot be the same."
            )
            return

        if os.path.exists(output_file):
            answer = QMessageBox.question(
                self,
                "Replace Existing File?",
                "That output file already exists.\n\n"
                "Do you want to replace it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )

            if answer != QMessageBox.Yes:
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
            "CONVERTING"
        )

        self.convert_button.setEnabled(False)
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
                "STOPPING"
            )

            self.worker.cancel()

    def update_progress(self, value):
        self.progress_bar.setValue(value)
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
            "DONE"
        )

        self.speed_label.setText(
            "Speed  Done"
        )
        self.eta_label.setText(
            "ETA  00:00"
        )

        self.convert_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        self.set_controls_enabled(True)
        self.open_folder_button.setEnabled(True)

        QMessageBox.information(
            self,
            "Conversion Complete",
            f"Your file is ready.\n\n{output_file}"
        )

    def conversion_cancelled(self):
        self.status_label.setText(
            "Conversion stopped"
        )

        self.status_pill.set_state(
            "idle",
            "READY"
        )

        self.speed_label.setText(
            "Speed  —"
        )
        self.eta_label.setText(
            "ETA  —"
        )

        self.convert_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.set_controls_enabled(True)

    def conversion_error(self, message):
        self.status_label.setText(
            "Conversion failed"
        )

        self.status_pill.set_state(
            "error",
            "ERROR"
        )

        self.convert_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        self.set_controls_enabled(True)

        QMessageBox.critical(
            self,
            "Conversion Error",
            message
        )

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
# MAIN WINDOW
# ============================================================

class MediaToolkitWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Media Toolkit")
        self.setMinimumSize(1000, 720)
        self.resize(1120, 800)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 20, 18, 18)
        side.setSpacing(8)

        brand = QHBoxLayout()
        icon = QLabel("M")
        icon.setObjectName("brandIcon")
        brand.addWidget(icon)
        text = QVBoxLayout()
        name = QLabel("MEDIA TOOLKIT")
        name.setObjectName("brandName")
        version = QLabel("EDITOR WORKSPACE")
        version.setObjectName("brandVersion")
        text.addWidget(name)
        text.addWidget(version)
        brand.addLayout(text)
        side.addLayout(brand)
        side.addSpacing(28)

        section = QLabel("WORKSPACE")
        section.setObjectName("sidebarLabel")
        side.addWidget(section)

        self.downloader_button = SidebarButton("↓", "Downloader")
        self.converter_button = SidebarButton("⇄", "Converter")
        self.downloader_button.clicked.connect(lambda: self.switch_page(0))
        self.converter_button.clicked.connect(lambda: self.switch_page(1))
        side.addWidget(self.downloader_button)
        side.addWidget(self.converter_button)
        side.addStretch()

        footer = QLabel("LOCAL TOOL\nFFmpeg + yt-dlp")
        footer.setObjectName("sidebarFooter")
        side.addWidget(footer)
        root.addWidget(sidebar)

        content = QFrame()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        topbar = QFrame()
        topbar.setObjectName("topbar")
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(28, 17, 28, 17)
        status = QLabel("●  LOCAL / READY")
        status.setObjectName("topStatus")
        top_layout.addStretch()
        top_layout.addWidget(status)
        content_layout.addWidget(topbar)

        self.pages = QStackedWidget()
        self.downloader_tab = DownloaderTab()
        self.converter_tab = ConverterTab()
        self.pages.addWidget(self.downloader_tab)
        self.pages.addWidget(self.converter_tab)
        content_layout.addWidget(self.pages, 1)
        root.addWidget(content, 1)

        self.switch_page(0)

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)
        self.downloader_button.setChecked(index == 0)
        self.converter_button.setChecked(index == 1)


# ============================================================
# STYLES
# ============================================================

STYLE = """
* { outline: none; }
QWidget {
    background: #0B0D10;
    color: #F3F5F8;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}
#sidebar { background: #090B0E; border-right: 1px solid #20242A; }
#content, #topbar { background: #111418; }
#pageScroll { background: #111418; border: 0; }
#converterPage { background: #111418; }
#topbar { border-bottom: 1px solid #20242A; }
#brandIcon {
    background: #F0F3F7; color: #0B0D10; border-radius: 9px;
    min-width: 34px; max-width: 34px; min-height: 34px; max-height: 34px;
    font-size: 17px; font-weight: 900; qproperty-alignment: AlignCenter;
}
#brandName { font-size: 12px; font-weight: 900; letter-spacing: 1.5px; }
#brandVersion, #sidebarLabel { color: #626A75; font-size: 9px; font-weight: 800; letter-spacing: 1.2px; }
#sidebarLabel { margin: 0 4px 5px 4px; }
#sidebarButton {
    text-align: left; padding: 0 13px; border: 1px solid transparent;
    border-radius: 10px; background: transparent; color: #737C88; font-weight: 750;
}
#sidebarButton:hover { background: #15191E; color: #E8EDF3; }
#sidebarButton:checked { background: #1B2026; border-color: #2C333B; color: #FFFFFF; }
#sidebarFooter { color: #4E5762; font-size: 10px; padding: 0 5px; line-height: 1.4; }
#topStatus { color: #626A75; font-size: 10px; font-weight: 800; letter-spacing: 1px; }
#pageTitle { font-size: 28px; font-weight: 850; color: #F6F8FB; }
#pageSubtitle { color: #7B8591; font-size: 13px; }
#eyebrow { color: #68727D; font-size: 10px; font-weight: 900; letter-spacing: 1.25px; }
#hint { color: #626C78; font-size: 10px; }
#card { background: #15191E; border: 1px solid #252B32; border-radius: 14px; }
#fieldLabel { color: #7F8995; font-size: 9px; font-weight: 800; letter-spacing: .7px; }
QLineEdit, QComboBox {
    background: #0E1115; border: 1px solid #2A3037; border-radius: 9px;
    padding: 8px 11px; min-height: 19px; color: #F0F3F7;
    selection-background-color: #486FD1;
}
QLineEdit:hover, QComboBox:hover { border-color: #3A434E; }
QLineEdit:focus, QComboBox:focus { border-color: #617FC5; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox QAbstractItemView { background: #15191E; color: #F0F3F7; border: 1px solid #343B44; selection-background-color: #2A447E; padding: 4px; }
QPushButton {
    background: #1B2026; color: #E8ECF1; border: 1px solid #303741;
    border-radius: 9px; padding: 10px 15px; font-weight: 750;
}
QPushButton:hover { background: #232930; border-color: #3E4853; }
QPushButton:pressed { background: #181C20; }
QPushButton:disabled { background: #161A1E; color: #525B65; border-color: #23282E; }
#primaryButton { background: #EEF2F6; color: #0B0D10; border: 1px solid #FFFFFF; font-weight: 900; }
#primaryButton:hover { background: #FFFFFF; }
#secondaryButton { background: #1A1F25; }
#ghostButton { background: transparent; border-color: transparent; color: #6F7884; }
#ghostButton:hover { background: #181C21; border-color: #252B32; color: #D6DCE4; }
#dangerButton { background: #2A171A; color: #FFB8BE; border-color: #51252B; }
#dangerButton:hover { background: #361B20; }
#statusLabel { color: #D8DEE6; font-weight: 750; }
#percentLabel { color: #A7B0BA; font-size: 12px; font-weight: 850; }
#mutedLabel { color: #616B76; font-size: 10px; }
#pathPreview { background: #0F1418; border: 1px solid #232A31; border-radius: 9px; color: #71808F; padding: 10px 12px; font-size: 10px; }
#statusPill { padding: 7px 11px; border-radius: 99px; min-width: 76px; background: #171C21; border: 1px solid #2B323A; color: #8F98A4; font-size: 9px; font-weight: 900; letter-spacing: 1px; }
#statusPill[state="working"] { background: #192338; border-color: #324D7D; color: #9EBEFF; }
#statusPill[state="success"] { background: #16281F; border-color: #2F5F46; color: #8DE0AE; }
#statusPill[state="error"] { background: #2A171A; border-color: #642A32; color: #FFAFB7; }
#dropZone { background: #101419; border: 1px dashed #39424D; border-radius: 12px; min-height: 130px; }
#dropZone:hover { background: #13181D; border-color: #56616D; }
#dropZone[dragging="true"] { background: #142039; border-color: #5D8EFF; }
#dropIcon { color: #858F9B; font-size: 26px; }
#dropTitle { color: #E7EBF1; font-size: 14px; font-weight: 800; }
#dropSubtitle { color: #68727D; font-size: 10px; }
QProgressBar { border: 0; border-radius: 5px; background: #0D1013; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: #718DFF; border-radius: 5px; }
"""


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    window = MediaToolkitWindow()
    window.show()
    sys.exit(app.exec())
