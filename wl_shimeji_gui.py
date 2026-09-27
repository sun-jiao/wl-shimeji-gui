#!/usr/bin/env python3
"""
wl_shimeji GUI
Qt6 / PySide6 frontend for wl_shimeji on Wayland / KDE Plasma.
Communicates with shimejictl and supports all mascot, prototype, environment,
configuration, and daemon management features.
"""

import errno
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt, QSize, QThread, Signal, Slot, QTimer
from PySide6.QtGui import QAction, QIcon, QPixmap, QImage, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox,
    QDoubleSpinBox, QSplitter, QStatusBar, QSystemTrayIcon, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QProgressBar
)

APP = 'wl_shimeji GUI'
APP_ID = 'wl-shimeji-gui'
HOME = Path.home()
DATA = HOME / '.local/share/wl_shimeji'
CONFIG = HOME / '.config/wl_shimeji'
AUTOSTART = HOME / '.config/autostart' / f'{APP_ID}.desktop'
DESKTOP = HOME / '.local/share/applications' / f'{APP_ID}.desktop'

# Global image cache to keep UI buttery smooth
_PIXMAP_CACHE = {}


def run_ctl_sync(*args, input_str=None, timeout=12):
    """
    Execute shimejictl using a pseudo-terminal (PTY).
    This prevents `shimejictl` from crashing on `os.get_terminal_size()`
    when called without an attached TTY.
    """
    master, slave = pty.openpty()
    try:
        p = subprocess.Popen(
            ['shimejictl', *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True
        )
        os.close(slave)
        if input_str:
            try:
                os.write(master, input_str.encode('utf-8'))
            except OSError:
                pass

        chunks = []
        while True:
            r, _, _ = select.select([master], [], [], 0.15)
            if r:
                try:
                    d = os.read(master, 4096)
                    if not d:
                        break
                    chunks.append(d)
                except OSError as e:
                    if e.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
            elif p.poll() is not None:
                # Process ended, drain remaining bytes
                while True:
                    try:
                        d = os.read(master, 4096)
                        if not d:
                            break
                        chunks.append(d)
                    except OSError:
                        break
                break

        p.wait(timeout=timeout)
        output = b''.join(chunks).decode('utf-8', errors='replace')
        output = output.replace('\r\n', '\n').replace('\r', '\n')
        return p.returncode, output
    finally:
        try:
            os.close(master)
        except OSError:
            pass


class CtlAsyncWorker(QThread):
    """
    Non-blocking worker thread for commands that stream output or wait for
    interactive clicks on screen (e.g. summon -s, dismiss --select, convert).
    """
    output_received = Signal(str)
    command_finished = Signal(int, str)

    def __init__(self, args, input_str=None, timeout=120, parent=None):
        super().__init__(parent)
        self.args = list(args)
        self.input_str = input_str
        self.timeout = timeout
        self._proc = None
        self._master = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def run(self):
        master, slave = pty.openpty()
        self._master = master
        chunks = []
        try:
            p = subprocess.Popen(
                ['shimejictl', *self.args],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True
            )
            self._proc = p
            os.close(slave)
            if self.input_str:
                try:
                    os.write(master, self.input_str.encode('utf-8'))
                except OSError:
                    pass

            while not self._cancelled:
                r, _, _ = select.select([master], [], [], 0.1)
                if r:
                    try:
                        d = os.read(master, 4096)
                        if not d:
                            break
                        text = d.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
                        chunks.append(d)
                        self.output_received.emit(text)
                    except OSError as e:
                        if e.errno in (errno.EIO, errno.EBADF):
                            break
                        raise
                elif p.poll() is not None:
                    # Drain remaining bytes
                    while True:
                        try:
                            d = os.read(master, 4096)
                            if not d:
                                break
                            text = d.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
                            chunks.append(d)
                            self.output_received.emit(text)
                        except OSError:
                            break
                    break

            if not self._cancelled:
                p.wait(timeout=self.timeout)
                returncode = p.returncode
            else:
                returncode = -1
                self.output_received.emit("\n[操作已被用户取消]\n")

            total_output = b''.join(chunks).decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
            self.command_finished.emit(returncode, total_output)
        except Exception as e:
            self.output_received.emit(f"\n[执行异常: {e}]\n")
            self.command_finished.emit(1, str(e))
        finally:
            try:
                os.close(master)
            except OSError:
                pass
            self._master = None
            self._proc = None


def load_pixmap(file_path):
    """Load an image file (.qoi, .png, etc.) and return a QPixmap."""
    if not file_path:
        return None
    file_path = Path(file_path)
    if not file_path.exists():
        return None

    cache_key = str(file_path.resolve())
    if cache_key in _PIXMAP_CACHE:
        return _PIXMAP_CACHE[cache_key]

    try:
        # PIL directly decodes QOI, PNG, WebP, JPEG
        im = Image.open(file_path).convert('RGBA')
        data = im.tobytes()
        qim = QImage(data, im.width, im.height, im.width * 4, QImage.Format.Format_RGBA8888)
        pix = QPixmap.fromImage(qim)
        if not pix.isNull():
            _PIXMAP_CACHE[cache_key] = pix
            return pix
    except Exception:
        pass

    # Fallback to direct QPixmap
    try:
        pix = QPixmap(str(file_path))
        if not pix.isNull():
            _PIXMAP_CACHE[cache_key] = pix
            return pix
    except Exception:
        pass
    return None


def find_preview(name):
    """
    Find preview sprite for a given prototype name.
    Supports both .qoi and .png files, searching wl_shimeji directories first.
    """
    needles = [name.lower(), ('shimeji.' + name).lower()]
    # Fast path: check DATA / 'shimejis' and DATA / 'prototypes'
    priority_roots = [DATA / 'shimejis', DATA / 'prototypes', DATA]
    candidates = []

    for root in priority_roots:
        if not root.exists():
            continue
        try:
            for ext in ('*.qoi', '*.png', '*.webp'):
                for p in root.rglob(ext):
                    s = str(p).lower()
                    if any(n in s for n in needles):
                        candidates.append(p)
        except OSError:
            pass
        if candidates:
            break

    # If still not found, search CONFIG
    if not candidates and CONFIG.exists():
        try:
            for ext in ('*.qoi', '*.png', '*.webp'):
                for p in CONFIG.rglob(ext):
                    s = str(p).lower()
                    if any(n in s for n in needles):
                        candidates.append(p)
        except OSError:
            pass

    if not candidates:
        return None

    def score(p):
        n = p.name.lower()
        if n in ('shime1.qoi', 'shime1.png', 'idle_01.qoi', 'idle_01.png', 'stand_01.qoi', 'stand_01.png'):
            return (0, len(str(p)))
        return (1, len(str(p)))

    return sorted(candidates, key=score)[0]


def prototype_names(text):
    """Extract clean prototype names from `shimejictl prototypes list` output."""
    names = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r'^[\\|`\-+*>\s]*\d+[.):]\s*', '', s)
        s = re.sub(r'^[\\|`\-+*>]\s*', '', s)
        low = s.lower()
        if s and not any(x in low for x in ['prototype', 'available', 'object id', 'end', 'id:', 'author:', 'version:']):
            names.append(s)
    return list(dict.fromkeys(names))


def parse_section(text, heading):
    """Parse list items under a given heading from `shimejictl prototypes info`."""
    values = []
    active = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == heading + ':':
            active = True
            continue
        if not active:
            continue
        if re.match(r'^[A-Za-z][A-Za-z _-]*:\s*', line) or 'Object id:' in line or '[END]' in line:
            break
        m = re.match(r'^[-*]\s+(.+?)\s*$', line)
        if m:
            values.append(m.group(1).strip())
    return list(dict.fromkeys(values))


def proto_info(name):
    """Retrieve behaviors and raw output for a prototype."""
    rc, out = run_ctl_sync('prototypes', 'info', name)
    behaviors = parse_section(out, 'Behaviors') if rc == 0 else []
    actions = parse_section(out, 'Actions') if rc == 0 else []
    return behaviors, actions, out


def prototype_store_path(info):
    """Parse 'Prototype store path: ...' from info text."""
    m = re.search(r'^\s*Prototype store path:\s*(.+?)\s*$', info, re.M)
    return m.group(1).strip() if m else ''


def parse_environments(text):
    """Parse environments from `shimejictl environment list` output."""
    envs = []
    pattern = re.compile(
        r'Environment\(\s*id=(?P<id>\d+)'
        r'(?:,\s*name=(?P<name>[^,]*))?'
        r'(?:,\s*description=(?P<desc>[^,]*))?'
        r'(?:,\s*pos=\((?P<x>-?\d+),(?P<y>-?\d+)\))?'
        r'(?:,\s*size=\((?P<w>\d+),(?P<h>\d+)\))?'
        r'(?:,\s*scale=(?P<scale>[\d.]+))?'
        r'(?:,\s*with\s+(?P<mascots>\d+)\s+mascots\))?'
    )
    for m in pattern.finditer(text):
        envs.append({
            'id': int(m.group('id')),
            'name': (m.group('name') or '').strip(),
            'description': (m.group('desc') or '').strip(),
            'pos': f"({m.group('x') or 0}, {m.group('y') or 0})",
            'size': f"{m.group('w') or '?'} x {m.group('h') or '?'}",
            'scale': f"{float(m.group('scale')):.2f}" if m.group('scale') else '1.00',
            'mascots': int(m.group('mascots') or 0)
        })
    return envs


class ConverterDialog(QDialog):
    """Dedicated dialog for converting Shimeji-EE instances to .wlshm format."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Shimeji-EE 格式转换工具 (Convert to wl_shimeji)')
        self.resize(600, 420)
        self.worker = None

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.src_edit = QLineEdit()
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_edit)
        btn_src_zip = QPushButton('选择 ZIP 压缩包')
        btn_src_dir = QPushButton('选择解压目录')
        src_row.addWidget(btn_src_zip)
        src_row.addWidget(btn_src_dir)
        form.addRow('源文件/目录:', src_row)

        self.out_edit = QLineEdit(str(Path.home() / 'Downloads'))
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit)
        btn_out_dir = QPushButton('浏览...')
        out_row.addWidget(btn_out_dir)
        form.addRow('输出保存目录:', out_row)

        self.force_check = QCheckBox('覆盖同名已有文件 (-f / --force)')
        self.force_check.setChecked(True)
        form.addRow('', self.force_check)

        self.auto_import = QCheckBox('转换完成后自动导入到角色库')
        self.auto_import.setChecked(True)
        form.addRow('', self.auto_import)

        layout.addLayout(form)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText('转换日志输出...')
        layout.addWidget(self.log_box)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton('开始转换')
        self.btn_cancel = QPushButton('取消')
        self.btn_close = QPushButton('关闭')
        btn_row.addStretch()
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_close)
        layout.addLayout(btn_row)

        btn_src_zip.clicked.connect(self.choose_zip)
        btn_src_dir.clicked.connect(self.choose_dir)
        btn_out_dir.clicked.connect(self.choose_out)
        self.btn_start.clicked.connect(self.start_conversion)
        self.btn_cancel.clicked.connect(self.cancel_conversion)
        self.btn_close.clicked.connect(self.accept)
        self.btn_cancel.setEnabled(False)

    def choose_zip(self):
        f, _ = QFileDialog.getOpenFileName(self, '选择 Shimeji-EE ZIP 压缩包', str(HOME), 'ZIP 压缩包 (*.zip)')
        if f:
            self.src_edit.setText(f)

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, '选择 Shimeji-EE 解压目录', str(HOME))
        if d:
            self.src_edit.setText(d)

    def choose_out(self):
        d = QFileDialog.getExistingDirectory(self, '选择输出目录', self.out_edit.text() or str(HOME))
        if d:
            self.out_edit.setText(d)

    def start_conversion(self):
        src = self.src_edit.text().strip()
        out = self.out_edit.text().strip()
        if not src or not os.path.exists(src):
            QMessageBox.warning(self, '错误', '请选择有效的源 ZIP 或目录。')
            return
        if not out:
            QMessageBox.warning(self, '错误', '请指定输出目录。')
            return
        os.makedirs(out, exist_ok=True)

        args = ['convert', src, '-O', out]
        if self.force_check.isChecked():
            args.append('-f')

        self.log_box.append(f"$ shimejictl {' '.join(args)}\n")
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        # Send 'A\n' to convert all prototypes if prompted
        self.worker = CtlAsyncWorker(args, input_str='A\n', parent=self)
        self.worker.output_received.connect(self.log_box.append)
        self.worker.command_finished.connect(self.on_conversion_finished)
        self.worker.start()

    def cancel_conversion(self):
        if self.worker:
            self.worker.cancel()

    def on_conversion_finished(self, rc, out):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if rc == 0:
            out_dir = Path(self.out_edit.text().strip())
            converted_files = sorted(str(p) for p in out_dir.glob('*.wlshm'))
            self.log_box.append(f"\n转换完成！输出目录包含 {len(converted_files)} 个 .wlshm 文件。\n")
            if self.auto_import.isChecked() and converted_files:
                import_rc, import_out = run_ctl_sync('prototypes', 'import', '-f', *converted_files)
                self.log_box.append(f"$ shimejictl prototypes import -f ...\n{import_out}\n")
                if import_rc == 0:
                    QMessageBox.information(self, '完成', f'已转换并成功导入 {len(converted_files)} 个角色。')
                    if self.parent() and hasattr(self.parent(), 'refresh'):
                        self.parent().refresh()
                else:
                    QMessageBox.warning(self, '提示', f'转换成功，但在导入角色时发生错误：\n{import_out[:1000]}')
            else:
                QMessageBox.information(self, '完成', '转换完成。')
        else:
            QMessageBox.warning(self, '转换失败', f'转换返回错误代码 {rc}。请查看日志。')


class SettingsDialog(QDialog):
    """
    Comprehensive settings dialog covering all keys supported by `shimejictl config`:
    - Interactions: BREEDING, DRAGGING, CURSOR_POSITION, WINDOW_INTERACTIONS, WINDOW_THROWING, WINDOW_THROW_POLICY, AFFORDANCES, DISMISS_ANIMATIONS
    - Multi-monitor: ALLOW_THROWING_MULTIHEAD, ALLOW_DRAGGING_MULTIHEAD, UNIFIED_OUTPUTS
    - Display & Scaling: MASCOT_SCALE, OPACITY, WLR_SHELL_LAYER, INTERPOLATION_FRAMERATE
    - Limits & Input: MASCOT_LIMIT, TABLETS_ENABLED, Autostart
    - Raw view
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('配置中心 - wl_shimeji 设置')
        self.resize(620, 520)

        vbox = QVBoxLayout(self)
        self.tabs = QTabWidget()
        vbox.addWidget(self.tabs)

        # Tab 1: 交互与行为
        t1 = QWidget()
        f1 = QFormLayout(t1)
        self.breed = QCheckBox('允许桌宠自我繁殖 (Multiplication / BREEDING)')
        self.drag = QCheckBox('允许拖拽桌宠 (Dragging / DRAGGING)')
        self.cursor = QCheckBox('跟踪鼠标光标位置 (Cursor Position / CURSOR_POSITION)')
        self.affordances = QCheckBox('启用角色专属交互特性 (Affordances / AFFORDANCES)')
        self.dismiss_anim = QCheckBox('启用消除退出动画 (Dismiss Animations / DISMISS_ANIMATIONS)')
        self.win_interact = QCheckBox('允许与应用程序窗口互动 (Window Interactions / WINDOW_INTERACTIONS)')
        self.win_throw = QCheckBox('允许抓取并投掷窗口 (Window Throwing / WINDOW_THROWING)')
        self.throw_policy = QComboBox()
        self.throw_policy.addItems(['looping', 'clamp', 'bounce', 'ignore'])

        f1.addRow(self.breed)
        f1.addRow(self.drag)
        f1.addRow(self.cursor)
        f1.addRow(self.affordances)
        f1.addRow(self.dismiss_anim)
        f1.addRow(self.win_interact)
        f1.addRow(self.win_throw)
        f1.addRow('窗口投掷出界策略:', self.throw_policy)
        self.tabs.addTab(t1, '交互与行为')

        # Tab 2: 多显示器与显示
        t2 = QWidget()
        f2 = QFormLayout(t2)
        self.throw_multi = QCheckBox('允许跨显示器投掷 (Allow Throwing Multihead)')
        self.drag_multi = QCheckBox('允许跨显示器拖拽 (Allow Dragging Multihead)')
        self.unified_out = QCheckBox('统一所有输出显示空间 (Unified Outputs)')

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(-1.0, 5.0)
        self.scale_spin.setSingleStep(0.25)
        self.scale_spin.setSpecialValueText('跟随系统缩放 (-1.0)')

        self.opacity_spin = QDoubleSpinBox()
        self.opacity_spin.setRange(-1.0, 1.0)
        self.opacity_spin.setSingleStep(0.1)
        self.opacity_spin.setSpecialValueText('完全不透明 (-1.0)')

        self.layer_combo = QComboBox()
        self.layer_combo.addItems(['overlay', 'top'])

        self.interp_spin = QSpinBox()
        self.interp_spin.setRange(-1, 1000)
        self.interp_spin.setSpecialValueText('跟随屏幕刷新率 (-1)')

        f2.addRow(self.throw_multi)
        f2.addRow(self.drag_multi)
        f2.addRow(self.unified_out)
        f2.addRow('角色缩放比例 (MASCOT_SCALE):', self.scale_spin)
        f2.addRow('角色不透明度 (OPACITY):', self.opacity_spin)
        f2.addRow('Wayland 图层 (WLR_SHELL_LAYER):', self.layer_combo)
        f2.addRow('插值帧率 (0=关闭, -1=跟随刷新率):', self.interp_spin)
        self.tabs.addTab(t2, '多屏与渲染')

        # Tab 3: 输入与系统
        t3 = QWidget()
        f3 = QFormLayout(t3)
        self.maxn = QSpinBox()
        self.maxn.setRange(1, 2048)
        self.maxn.setValue(512)
        self.tablets = QCheckBox('启用数位板/手写板输入 (KWin 建议关闭)')
        self.autostart = QCheckBox('登录 KDE Plasma 时自动启动 GUI')
        self.autostart.setChecked(AUTOSTART.exists())

        f3.addRow('最大桌宠总数 (MASCOT_LIMIT):', self.maxn)
        f3.addRow(self.tablets)
        f3.addRow(self.autostart)
        self.tabs.addTab(t3, '系统与输入')

        # Tab 4: 完整原始配置
        t4 = QWidget()
        v4 = QVBoxLayout(t4)
        self.raw_text = QTextEdit()
        self.raw_text.setReadOnly(True)
        v4.addWidget(self.raw_text)
        self.tabs.addTab(t4, '完整配置')

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_reload = QPushButton('重新读取')
        btn_save = QPushButton('应用并保存')
        btn_close = QPushButton('关闭')
        btn_row.addWidget(btn_reload)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_close)
        vbox.addLayout(btn_row)

        btn_reload.clicked.connect(self.load_config)
        btn_save.clicked.connect(self.apply_config)
        btn_close.clicked.connect(self.accept)

        self.load_config()

    def load_config(self):
        rc, out = run_ctl_sync('config', 'list')
        self.raw_text.setPlainText(out)

        vals = {}
        for line in out.splitlines():
            # e.g.: - Multiplication (BREEDING) = true
            m = re.search(r'\(([^)]+)\)\s*=\s*(.+)$', line)
            if m:
                vals[m.group(1).strip()] = m.group(2).strip()

        def parse_bool(key, default=False):
            return vals.get(key, str(default)).lower() in ('1', 'true', 'yes', 'on')

        self.breed.setChecked(parse_bool('BREEDING', True))
        self.drag.setChecked(parse_bool('DRAGGING', True))
        self.cursor.setChecked(parse_bool('CURSOR_POSITION', True))
        self.affordances.setChecked(parse_bool('AFFORDANCES', True))
        self.dismiss_anim.setChecked(parse_bool('DISMISS_ANIMATIONS', True))
        self.win_interact.setChecked(parse_bool('WINDOW_INTERACTIONS', False))
        self.win_throw.setChecked(parse_bool('WINDOW_THROWING', False))

        policy = vals.get('WINDOW_THROW_POLICY', 'looping').lower()
        idx = self.throw_policy.findText(policy)
        if idx >= 0:
            self.throw_policy.setCurrentIndex(idx)

        self.throw_multi.setChecked(parse_bool('ALLOW_THROWING_MULTIHEAD', True))
        self.drag_multi.setChecked(parse_bool('ALLOW_DRAGGING_MULTIHEAD', True))
        self.unified_out.setChecked(parse_bool('UNIFIED_OUTPUTS', True))

        try:
            self.scale_spin.setValue(float(vals.get('MASCOT_SCALE', -1.0)))
        except ValueError:
            self.scale_spin.setValue(-1.0)

        try:
            self.opacity_spin.setValue(float(vals.get('OPACITY', -1.0)))
        except ValueError:
            self.opacity_spin.setValue(-1.0)

        layer = vals.get('WLR_SHELL_LAYER', 'overlay').lower()
        idx_layer = self.layer_combo.findText(layer)
        if idx_layer >= 0:
            self.layer_combo.setCurrentIndex(idx_layer)

        try:
            self.interp_spin.setValue(int(vals.get('INTERPOLATION_FRAMERATE', 0)))
        except ValueError:
            self.interp_spin.setValue(0)

        try:
            self.maxn.setValue(int(vals.get('MASCOT_LIMIT', 512)))
        except ValueError:
            self.maxn.setValue(512)

        self.tablets.setChecked(parse_bool('TABLETS_ENABLED', False))

    def apply_config(self):
        settings_map = {
            'BREEDING': str(self.breed.isChecked()).lower(),
            'DRAGGING': str(self.drag.isChecked()).lower(),
            'CURSOR_POSITION': str(self.cursor.isChecked()).lower(),
            'AFFORDANCES': str(self.affordances.isChecked()).lower(),
            'DISMISS_ANIMATIONS': str(self.dismiss_anim.isChecked()).lower(),
            'WINDOW_INTERACTIONS': str(self.win_interact.isChecked()).lower(),
            'WINDOW_THROWING': str(self.win_throw.isChecked()).lower(),
            'WINDOW_THROW_POLICY': self.throw_policy.currentText(),
            'ALLOW_THROWING_MULTIHEAD': str(self.throw_multi.isChecked()).lower(),
            'ALLOW_DRAGGING_MULTIHEAD': str(self.drag_multi.isChecked()).lower(),
            'UNIFIED_OUTPUTS': str(self.unified_out.isChecked()).lower(),
            'MASCOT_SCALE': f"{self.scale_spin.value():.2f}",
            'OPACITY': f"{self.opacity_spin.value():.2f}",
            'WLR_SHELL_LAYER': self.layer_combo.currentText(),
            'INTERPOLATION_FRAMERATE': str(self.interp_spin.value()),
            'MASCOT_LIMIT': str(self.maxn.value()),
            'TABLETS_ENABLED': str(self.tablets.isChecked()).lower(),
        }

        errors = []
        for key, val in settings_map.items():
            rc, out = run_ctl_sync('config', 'set', key, val)
            if rc != 0:
                errors.append(f"{key}: {out.strip()}")

        # Update autostart desktop entry
        try:
            AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
            if self.autostart.isChecked():
                if DESKTOP.exists():
                    shutil.copy2(DESKTOP, AUTOSTART)
                else:
                    AUTOSTART.write_text(
                        '[Desktop Entry]\n'
                        'Type=Application\n'
                        'Name=wl_shimeji GUI\n'
                        'Exec=wl-shimeji-gui\n'
                        'Icon=applications-games\n'
                        'X-KDE-autostart-after=panel\n',
                        encoding='utf-8'
                    )
            elif AUTOSTART.exists():
                AUTOSTART.unlink()
        except Exception as e:
            errors.append(f"自启动配置失败: {e}")

        self.load_config()
        if not errors:
            QMessageBox.information(self, '设置', '配置已成功应用。\n部分底层 Overlay 参数在重启桌宠服务后完全生效。')
        else:
            QMessageBox.warning(self, '设置', '部分配置写入失败：\n' + '\n'.join(errors))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.resize(920, 680)

        self.names = []
        self.environments = []
        self.current_worker = None

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        # ================= Left Pane: 角色库 =================
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('搜索角色...')
        self.search_input.setClearButtonEnabled(True)
        search_row.addWidget(self.search_input)
        left_layout.addLayout(search_row)

        self.proto_list = QListWidget()
        self.proto_list.setIconSize(QSize(64, 64))
        self.proto_list.setMinimumWidth(260)
        left_layout.addWidget(self.proto_list)

        left_btn_row1 = QHBoxLayout()
        self.btn_refresh = QPushButton('刷新')
        self.btn_reload_all = QPushButton('热重载角色')
        self.btn_reload_all.setToolTip('向守护进程发送重载信号 (prototypes reload-all)')
        left_btn_row1.addWidget(self.btn_refresh)
        left_btn_row1.addWidget(self.btn_reload_all)
        left_layout.addLayout(left_btn_row1)

        left_btn_row2 = QHBoxLayout()
        self.btn_add = QPushButton('添加角色')
        self.btn_export = QPushButton('导出')
        self.btn_delete = QPushButton('删除角色')
        left_btn_row2.addWidget(self.btn_add)
        left_btn_row2.addWidget(self.btn_export)
        left_btn_row2.addWidget(self.btn_delete)
        left_layout.addLayout(left_btn_row2)

        splitter.addWidget(left_widget)

        # ================= Right Pane: 标签页 =================
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs)

        # --- Tab 1: 角色与召唤控制 ---
        tab_summon = QWidget()
        ts_layout = QVBoxLayout(tab_summon)

        # Preview & Overview Row
        overview_box = QHBoxLayout()
        self.preview_label = QLabel('选择角色')
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(180, 160)
        self.preview_label.setStyleSheet("border: 1px dashed #666; border-radius: 6px;")
        overview_box.addWidget(self.preview_label, 1)

        info_layout = QVBoxLayout()
        self.role_title = QLabel('未选择角色')
        font = self.role_title.font()
        font.setPointSize(14)
        font.setBold(True)
        self.role_title.setFont(font)
        self.role_meta = QLabel('')
        self.role_meta.setStyleSheet('color: #888;')

        beh_form = QFormLayout()
        self.beh_combo = QComboBox()
        beh_form.addRow('指定初始行为:', self.beh_combo)

        info_layout.addWidget(self.role_title)
        info_layout.addWidget(self.role_meta)
        info_layout.addLayout(beh_form)
        info_layout.addStretch()
        overview_box.addLayout(info_layout, 2)
        ts_layout.addLayout(overview_box)

        # Group 1: 召唤桌宠
        grp_summon = QGroupBox('召唤桌宠 (Summon)')
        gs_layout = QVBoxLayout(grp_summon)

        row_s1 = QHBoxLayout()
        row_s1.addWidget(QLabel('数量:'))
        self.summon_count = QSpinBox()
        self.summon_count.setRange(1, 50)
        self.summon_count.setValue(1)
        row_s1.addWidget(self.summon_count)

        row_s1.addWidget(QLabel('目标屏幕:'))
        self.summon_env_combo = QComboBox()
        self.summon_env_combo.addItem('随机 / 全部环境', '')
        row_s1.addWidget(self.summon_env_combo, 1)

        row_s1.addWidget(QLabel('坐标 X:'))
        self.pos_x = QSpinBox()
        self.pos_x.setRange(-1, 9999)
        self.pos_x.setValue(-1)
        self.pos_x.setSpecialValueText('随机 (-1)')
        row_s1.addWidget(self.pos_x)

        row_s1.addWidget(QLabel('Y:'))
        self.pos_y = QSpinBox()
        self.pos_y.setRange(-1, 9999)
        self.pos_y.setValue(-1)
        self.pos_y.setSpecialValueText('默认 (-1)')
        row_s1.addWidget(self.pos_y)

        gs_layout.addLayout(row_s1)

        row_s2 = QHBoxLayout()
        self.btn_summon_direct = QPushButton('立即召唤')
        self.btn_summon_direct.setStyleSheet('font-weight: bold;')
        self.btn_summon_select = QPushButton('屏幕点选位置召唤 (-s)')
        row_s2.addWidget(self.btn_summon_direct)
        row_s2.addWidget(self.btn_summon_select)
        gs_layout.addLayout(row_s2)

        ts_layout.addWidget(grp_summon)

        # Group 2: 消除控制
        grp_dismiss = QGroupBox('消除与清理 (Dismiss)')
        gd_layout = QVBoxLayout(grp_dismiss)

        row_d1 = QHBoxLayout()
        self.btn_dismiss_select = QPushButton('点选并删除一只 (-s)')
        self.btn_dismiss_same = QPushButton('点选并删除同类 (-fs -s)')
        self.btn_dismiss_other = QPushButton('点选并删除其它 (-o -s)')
        row_d1.addWidget(self.btn_dismiss_select)
        row_d1.addWidget(self.btn_dismiss_same)
        row_d1.addWidget(self.btn_dismiss_other)
        gd_layout.addLayout(row_d1)

        row_d2 = QHBoxLayout()
        self.btn_dismiss_env = QPushButton('清除所选屏幕桌宠')
        self.btn_dismiss_all = QPushButton('删除全部桌宠 (--all)')
        self.btn_dismiss_all.setStyleSheet('color: #d9534f; font-weight: bold;')

        row_d2.addWidget(QLabel('桌宠 ID:'))
        self.dismiss_id_input = QLineEdit()
        self.dismiss_id_input.setPlaceholderText('例如: 1')
        self.dismiss_id_input.setMaximumWidth(80)
        self.btn_dismiss_by_id = QPushButton('按 ID 删除')
        row_d2.addWidget(self.dismiss_id_input)
        row_d2.addWidget(self.btn_dismiss_by_id)
        row_d2.addWidget(self.btn_dismiss_env)
        row_d2.addWidget(self.btn_dismiss_all)
        gd_layout.addLayout(row_d2)

        ts_layout.addWidget(grp_dismiss)

        # Group 3: 行为切换与窗口救援
        grp_extra = QGroupBox('行为修改与窗口急救')
        ge_layout = QHBoxLayout(grp_extra)
        self.btn_set_behavior_select = QPushButton('点选桌宠切换为此行为')
        self.btn_restore_windows = QPushButton('恢复被抓取的窗口 (Restore Windows)')
        self.btn_restore_windows.setToolTip('桌宠抓取或投掷程序窗口后，可用此命令恢复所有窗口到正常桌面视野。')
        self.btn_restore_windows.setStyleSheet('background-color: #2b5b84; color: white; font-weight: bold; padding: 6px;')

        ge_layout.addWidget(self.btn_set_behavior_select)
        ge_layout.addWidget(self.btn_restore_windows)
        ts_layout.addWidget(grp_extra)

        self.tabs.addTab(tab_summon, '角色与控制')

        # --- Tab 2: 屏幕环境管理 ---
        tab_env = QWidget()
        te_layout = QVBoxLayout(tab_env)

        te_hint = QLabel('wl_shimeji 在 Wayland 输出上运行独立的屏幕环境 (Environment)。')
        te_hint.setStyleSheet('color: #888;')
        te_layout.addWidget(te_hint)

        self.env_table = QTableWidget(0, 6)
        self.env_table.setHorizontalHeaderLabels(['环境 ID', '分辨率 (WxH)', '桌面坐标', '缩放比例', '当前桌宠数', '描述'])
        self.env_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.env_table.setSelectionBehavior(QTableWidget.SelectRows)
        te_layout.addWidget(self.env_table)

        env_btn_row = QHBoxLayout()
        self.btn_refresh_env = QPushButton('刷新屏幕列表')
        self.btn_close_env_selected = QPushButton('关闭所选屏幕覆盖层 (-i ID)')
        self.btn_close_env_select = QPushButton('屏幕点选关闭覆盖层')
        env_btn_row.addWidget(self.btn_refresh_env)
        env_btn_row.addWidget(self.btn_close_env_selected)
        env_btn_row.addWidget(self.btn_close_env_select)
        env_btn_row.addStretch()
        te_layout.addLayout(env_btn_row)

        self.tabs.addTab(tab_env, '屏幕环境 (Environments)')

        # --- Tab 3: 角色属性详情 ---
        tab_inspect = QWidget()
        ti_layout = QVBoxLayout(tab_inspect)
        self.inspect_text = QTextEdit()
        self.inspect_text.setReadOnly(True)
        ti_layout.addWidget(self.inspect_text)
        self.tabs.addTab(tab_inspect, '角色详情 (Inspector)')

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # ================= Bottom Area: 日志与全局控制 =================
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel('执行日志 (Command Output):'))
        self.btn_cancel_worker = QPushButton('中断当前命令')
        self.btn_cancel_worker.setEnabled(False)
        self.btn_clear_log = QPushButton('清空日志')
        log_header.addStretch()
        log_header.addWidget(self.btn_cancel_worker)
        log_header.addWidget(self.btn_clear_log)
        main_layout.addLayout(log_header)

        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        self.log_console.setMaximumHeight(130)
        self.log_console.setStyleSheet('font-family: monospace; font-size: 11px;')
        main_layout.addWidget(self.log_console)

        bottom_bar = QHBoxLayout()
        self.status_label = QLabel('就绪')
        bottom_bar.addWidget(self.status_label, 1)

        self.btn_converter = QPushButton('Shimeji-EE 转换工具')
        self.btn_stop_daemon = QPushButton('停止后台服务')
        self.btn_restart_daemon = QPushButton('重启后台服务')
        self.btn_settings = QPushButton('设置...')

        bottom_bar.addWidget(self.btn_converter)
        bottom_bar.addWidget(self.btn_stop_daemon)
        bottom_bar.addWidget(self.btn_restart_daemon)
        bottom_bar.addWidget(self.btn_settings)
        main_layout.addLayout(bottom_bar)

        # ================= Connect Signals =================
        self.search_input.textChanged.connect(self.filter_prototypes)
        self.proto_list.currentItemChanged.connect(self.on_proto_selected)
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_reload_all.clicked.connect(self.reload_all_prototypes)
        self.btn_add.clicked.connect(self.add_role)
        self.btn_export.clicked.connect(self.export_role)
        self.btn_delete.clicked.connect(self.delete_role)

        self.btn_summon_direct.clicked.connect(lambda: self.summon(select=False))
        self.btn_summon_select.clicked.connect(lambda: self.summon(select=True))
        self.btn_dismiss_select.clicked.connect(lambda: self.run_async(['mascot', 'dismiss', '-s'], '正在等待屏幕点选以消除桌宠...'))
        self.btn_dismiss_same.clicked.connect(lambda: self.run_async(['mascot', 'dismiss', '-fs', '-s'], '正在等待屏幕点选以消除所有同类桌宠...'))
        self.btn_dismiss_other.clicked.connect(lambda: self.run_async(['mascot', 'dismiss', '-o', '-s'], '正在等待屏幕点选以消除其它桌宠...'))
        self.btn_dismiss_all.clicked.connect(self.dismiss_all)
        self.btn_dismiss_by_id.clicked.connect(self.dismiss_by_id)
        self.btn_dismiss_env.clicked.connect(self.dismiss_by_env)

        self.btn_set_behavior_select.clicked.connect(self.change_behavior_select)
        self.btn_restore_windows.clicked.connect(self.restore_windows)

        self.btn_refresh_env.clicked.connect(self.refresh_environments)
        self.btn_close_env_selected.clicked.connect(self.close_selected_environment)
        self.btn_close_env_select.clicked.connect(lambda: self.run_async(['environment', 'close'], '正在等待屏幕点选以关闭该屏幕覆盖层...'))

        self.btn_cancel_worker.clicked.connect(self.cancel_current_command)
        self.btn_clear_log.clicked.connect(self.log_console.clear)

        self.btn_converter.clicked.connect(lambda: ConverterDialog(self).exec())
        self.btn_stop_daemon.clicked.connect(self.stop_daemon)
        self.btn_restart_daemon.clicked.connect(self.restart_daemon)
        self.btn_settings.clicked.connect(lambda: SettingsDialog(self).exec())

        self.setup_tray()
        self.refresh()
        self.refresh_environments()

    def log(self, text):
        self.log_console.append(text.rstrip())

    def run_async(self, args, status_msg='正在执行命令...', callback=None):
        """Run shimejictl command asynchronously with live logging."""
        if self.current_worker and self.current_worker.isRunning():
            QMessageBox.warning(self, '提示', '前一个命令正在执行，请先等待完成或点击“中断当前命令”。')
            return

        self.status_label.setText(status_msg)
        self.btn_cancel_worker.setEnabled(True)
        self.log(f"$ shimejictl {' '.join(str(x) for x in args)}")

        self.current_worker = CtlAsyncWorker(args, parent=self)
        self.current_worker.output_received.connect(self.log)

        def on_done(rc, out):
            self.btn_cancel_worker.setEnabled(False)
            self.status_label.setText(f'命令执行完成 (返回码: {rc})')
            if callback:
                callback(rc, out)
            self.refresh_environments()

        self.current_worker.command_finished.connect(on_done)
        self.current_worker.start()

    def cancel_current_command(self):
        if self.current_worker:
            self.current_worker.cancel()
            self.status_label.setText('已请求中断当前命令')

    def current_prototype_name(self):
        item = self.proto_list.currentItem()
        return item.data(Qt.UserRole) if item else ''

    def refresh(self):
        """Fetch prototype names and populate list with previews."""
        rc, out = run_ctl_sync('prototypes', 'list')
        self.proto_list.clear()
        self.names = []

        if rc != 0:
            self.status_label.setText('无法连接或读取角色列表；请确认 shimejictl 已安装。')
            self.log(f"读取角色失败:\n{out}")
            return

        self.names = prototype_names(out)
        filter_text = self.search_input.text().strip().lower()

        for name in self.names:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            preview_path = find_preview(name)
            pix = load_pixmap(preview_path)
            if pix:
                item.setIcon(QIcon(pix))
            else:
                item.setIcon(QIcon.fromTheme('applications-games'))

            if not filter_text or filter_text in name.lower():
                item.setHidden(False)
            else:
                item.setHidden(True)
            self.proto_list.addItem(item)

        if self.proto_list.count() > 0:
            for i in range(self.proto_list.count()):
                if not self.proto_list.item(i).isHidden():
                    self.proto_list.setCurrentRow(i)
                    break

        self.status_label.setText(f'已载入 {len(self.names)} 个角色原型')
        self.rebuild_tray()

    def filter_prototypes(self, text):
        text = text.strip().lower()
        for i in range(self.proto_list.count()):
            item = self.proto_list.item(i)
            name = item.data(Qt.UserRole) or ''
            item.setHidden(bool(text and text not in name.lower()))

    def on_proto_selected(self, item, *args):
        self.beh_combo.clear()
        self.beh_combo.addItem('(默认行为)', '')

        if not item:
            self.role_title.setText('未选择角色')
            self.role_meta.setText('')
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText('选择角色')
            self.inspect_text.clear()
            return

        name = item.data(Qt.UserRole)
        self.role_title.setText(name)

        behaviors, actions, raw_info = proto_info(name)
        for b in behaviors:
            self.beh_combo.addItem(b, b)

        author_m = re.search(r'^\s*Author:\s*(.*?)$', raw_info, re.M)
        ver_m = re.search(r'^\s*Version:\s*(.*?)$', raw_info, re.M)
        author = author_m.group(1).strip() if author_m else ''
        version = ver_m.group(1).strip() if ver_m else '0'

        meta_parts = []
        if author:
            meta_parts.append(f"作者: {author}")
        if version:
            meta_parts.append(f"版本: {version}")
        meta_parts.append(f"动作数: {len(actions)}")
        meta_parts.append(f"行为数: {len(behaviors)}")
        self.role_meta.setText(' | '.join(meta_parts))

        # Update preview label
        preview_path = find_preview(name)
        pix = load_pixmap(preview_path)
        if pix:
            self.preview_label.setText('')
            self.preview_label.setPixmap(
                pix.scaled(200, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"{name}\n\n(未找到本地预览图)")

        # Update Inspector Tab
        inspect_lines = [
            f"=== 角色原型: {name} ===",
            f"作者 (Author): {author or '(无)'}",
            f"版本 (Version): {version}",
            f"存储路径 (Store Path): {prototype_store_path(raw_info) or '(未提供)'}",
            f"\n--- 可用动作集 (Actions, 共 {len(actions)} 种) ---",
        ]
        for a in actions:
            inspect_lines.append(f"  • {a}")
        inspect_lines.append(f"\n--- 可用行为集 (Behaviors, 共 {len(behaviors)} 种) ---")
        for b in behaviors:
            inspect_lines.append(f"  • {b}")
        inspect_lines.append("\n--- 原始底层信息 (shimejictl prototypes info) ---")
        inspect_lines.append(raw_info)
        self.inspect_text.setPlainText('\n'.join(inspect_lines))

    def summon(self, select=False):
        name = self.current_prototype_name()
        if not name:
            QMessageBox.information(self, '提示', '请先在左侧选择一个角色。')
            return

        count = self.summon_count.value()
        behavior = self.beh_combo.currentData() or ''
        env_id = self.summon_env_combo.currentData()
        x_val = self.pos_x.value()
        y_val = self.pos_y.value()

        base_args = ['mascot', 'summon']
        if select:
            base_args.append('-s')
        if behavior:
            base_args.extend(['-b', behavior])
        if env_id:
            base_args.extend(['-e', str(env_id)])

        if not select:
            if x_val >= 0 and y_val >= 0:
                base_args.extend(['--position', f'{x_val},{y_val}'])
            elif x_val >= 0:
                base_args.extend(['-x', str(x_val)])
            elif y_val >= 0:
                base_args.extend(['-y', str(y_val)])

        base_args.append(name)

        if select or count == 1:
            self.run_async(base_args, '请在屏幕上点击要召唤桌宠的位置...' if select else '正在召唤桌宠...')
        else:
            # Batch summon
            self.status_label.setText(f'正在批量召唤 {count} 只 {name}...')
            self.log(f"批量召唤 {count} 只 {name}...")

            def batch_thread():
                for i in range(count):
                    run_ctl_sync(*base_args)
                QTimer.singleShot(0, lambda: [
                    self.status_label.setText(f'已完成召唤 {count} 只 {name}'),
                    self.refresh_environments()
                ])

            threading.Thread(target=batch_thread, daemon=True).start()

    def dismiss_all(self):
        if QMessageBox.question(self, '确认', '确定要清除屏幕上的全部桌宠吗？') == QMessageBox.Yes:
            self.run_async(['mascot', 'dismiss', '--all'], '正在清除全部桌宠...')

    def dismiss_by_id(self):
        mid = self.dismiss_id_input.text().strip()
        if not mid or not mid.isdigit():
            QMessageBox.warning(self, '错误', '请输入纯数字的桌宠 ID。')
            return
        self.run_async(['mascot', 'dismiss', '-i', mid], f'正在删除桌宠 #{mid}...')

    def dismiss_by_env(self):
        env_id = self.summon_env_combo.currentData()
        if not env_id:
            # Try getting selected row from environment table
            row = self.env_table.currentRow()
            if row >= 0:
                item = self.env_table.item(row, 0)
                if item:
                    env_id = item.text().strip()
        if not env_id:
            QMessageBox.information(self, '提示', '请先在“目标屏幕”下拉框或“屏幕环境”标签页中选中一个具体屏幕。')
            return
        self.run_async(['mascot', 'dismiss', '-e', str(env_id), '--all'], f'正在清除屏幕 #{env_id} 上的所有桌宠...')

    def change_behavior_select(self):
        beh = self.beh_combo.currentData() or self.beh_combo.currentText()
        if not beh or beh == '(默认行为)':
            QMessageBox.information(self, '提示', '请先在“指定初始行为”下拉框选择一个具体行为。')
            return
        self.run_async(['mascot', 'set-behavior', beh], f'请在屏幕上点选要切换为「{beh}」的桌宠...')

    def restore_windows(self):
        self.run_async(['restore-windows'], '正在恢复被抓取的窗口 (restore-windows)...')

    def reload_all_prototypes(self):
        self.run_async(['prototypes', 'reload-all'], '正在向守护进程发送角色重载信号...', callback=lambda rc, _: self.refresh())

    def add_role(self):
        f, _ = QFileDialog.getOpenFileName(
            self, '添加角色', str(HOME),
            'Shimeji 角色 (*.wlshm *.zip);;wl_shimeji 角色包 (*.wlshm);;Shimeji-EE ZIP (*.zip);;所有文件 (*)'
        )
        if not f:
            return

        if f.lower().endswith('.wlshm'):
            self.run_async(['prototypes', 'import', '-f', f], f'正在导入 {Path(f).name}...', callback=lambda rc, _: self.refresh())
            return

        # It is a zip archive: use temporary directory to convert and then import
        with tempfile.TemporaryDirectory(prefix='wl-shimeji-gui-') as td:
            p = subprocess.run(['shimejictl', 'convert', f, '-O', td, '-f'], input='A\n', text=True, capture_output=True)
            out = (p.stdout or '') + (p.stderr or '')
            self.log(f"$ shimejictl convert {f} -O <temp> -f\n{out}")
            files = sorted(str(x) for x in Path(td).glob('*.wlshm'))
            if p.returncode or not files:
                QMessageBox.warning(self, '添加角色', '转换失败，或 ZIP 中未找到有效的 Shimeji-EE 资源。\n\n' + out[-2000:])
                return
            rc, imp_out = run_ctl_sync('prototypes', 'import', '-f', *files)
            self.log(f"$ shimejictl prototypes import -f ...\n{imp_out}")
            if rc == 0:
                self.refresh()
                QMessageBox.information(self, '成功', f'已成功转换并导入 {len(files)} 个角色原型！')
            else:
                QMessageBox.warning(self, '导入失败', f'转换成功但在导入时发生错误：\n{imp_out}')

    def export_role(self):
        name = self.current_prototype_name()
        if not name:
            QMessageBox.information(self, '提示', '请先在左侧选择要导出的角色。')
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, f'导出角色 - {name}', str(HOME / f'Shimeji.{name}.wlshm'), 'wl_shimeji 角色包 (*.wlshm)'
        )
        if dest:
            self.run_async(['prototypes', 'export', '-i', name, '-o', dest, '-f'], f'正在导出 {name} 到 {dest}...')

    def delete_role(self):
        name = self.current_prototype_name()
        if not name:
            return
        if QMessageBox.question(self, '删除角色', f'确定从 wl_shimeji 角色库中彻底删除「{name}」吗？\n\n注意：这不会影响您原来下载的 ZIP 或 .wlshm 文件。') != QMessageBox.Yes:
            return

        # Check if native remove command exists
        rc, helpout = run_ctl_sync('prototypes', '--help')
        for verb in ('remove', 'delete'):
            if re.search(rf'(?<![\w-]){verb}(?![\w-])', helpout):
                self.run_async(['prototypes', verb, name], f'正在删除 {name}...', callback=lambda rc, _: self.refresh())
                return

        # Fallback to store path deletion
        behaviors, actions, info = proto_info(name)
        rel = prototype_store_path(info)
        if not rel:
            QMessageBox.warning(self, '删除角色', '无法解析该角色的 Prototype store path。')
            return

        # Candidate target locations
        target = None
        for base in [DATA / 'shimejis', DATA / 'prototypes', DATA]:
            candidate = (base / rel).resolve()
            if candidate.exists():
                target = candidate
                break

        if not target or not target.exists():
            QMessageBox.warning(self, '删除角色', f'未在本地数据目录中找到角色存储文件夹：\n{rel}')
            return

        # Safety check: must reside inside ~/.local/share/wl_shimeji
        try:
            target.relative_to(DATA.resolve())
        except ValueError:
            QMessageBox.critical(self, '安全限制', '拒绝删除：路径不在 wl_shimeji 专属目录内。')
            return

        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            self.log(f"已清理角色存储目录: {target}")
            # Hot reload or stop overlay to drop memory references
            run_ctl_sync('prototypes', 'reload-all')
            self.refresh()
            QMessageBox.information(self, '删除角色', f'角色「{name}」已成功移除。')
        except OSError as e:
            QMessageBox.critical(self, '删除角色失败', f"文件系统操作失败: {e}")

    def refresh_environments(self):
        """Query `shimejictl environment list` and update table & combo boxes."""
        rc, out = run_ctl_sync('environment', 'list')
        if rc != 0:
            return

        self.environments = parse_environments(out)
        self.env_table.setRowCount(len(self.environments))

        # Update environment combobox in summon tab
        prev_env = self.summon_env_combo.currentData()
        self.summon_env_combo.clear()
        self.summon_env_combo.addItem('随机 / 全部环境', '')

        for row, env in enumerate(self.environments):
            eid_str = str(env['id'])
            self.env_table.setItem(row, 0, QTableWidgetItem(eid_str))
            self.env_table.setItem(row, 1, QTableWidgetItem(env['size']))
            self.env_table.setItem(row, 2, QTableWidgetItem(env['pos']))
            self.env_table.setItem(row, 3, QTableWidgetItem(env['scale']))
            self.env_table.setItem(row, 4, QTableWidgetItem(str(env['mascots'])))
            self.env_table.setItem(row, 5, QTableWidgetItem(env['name'] or env['description'] or '-'))

            label = f"屏幕 #{env['id']} ({env['size']}, {env['mascots']} 只桌宠)"
            self.summon_env_combo.addItem(label, env['id'])

        if prev_env:
            idx = self.summon_env_combo.findData(prev_env)
            if idx >= 0:
                self.summon_env_combo.setCurrentIndex(idx)

    def close_selected_environment(self):
        row = self.env_table.currentRow()
        if row < 0:
            QMessageBox.information(self, '提示', '请在表格中选择要关闭覆盖层的屏幕。')
            return
        item = self.env_table.item(row, 0)
        if not item:
            return
        eid = item.text().strip()
        if QMessageBox.question(
            self, '确认',
            f'确定关闭屏幕 #{eid} 的覆盖层吗？\n(这相当于拔出该显示器输出，若要恢复该屏幕覆盖层需要重启桌宠守护进程。)'
        ) == QMessageBox.Yes:
            # shimejictl environment close without -i triggers interactive selection,
            # or with -i passes ID
            self.run_async(['environment', 'close', '-i', eid], f'正在关闭屏幕 #{eid} 覆盖层...')

    def stop_daemon(self):
        if QMessageBox.question(self, '停止服务', '确定停止 wl_shimeji 守护进程吗？所有屏幕上的桌宠将被退出。') == QMessageBox.Yes:
            self.run_async(['stop'], '正在停止后台守护进程...', callback=lambda rc, _: self.refresh_environments())

    def restart_daemon(self):
        self.status_label.setText('正在重启 wl_shimeji 守护进程...')
        self.log('$ shimejictl stop && restart')

        def restart_thread():
            run_ctl_sync('stop')
            import time
            time.sleep(1.0)
            run_ctl_sync('environment', 'list')
            QTimer.singleShot(0, lambda: [
                self.status_label.setText('后台服务已重启完成'),
                self.refresh(),
                self.refresh_environments()
            ])

        threading.Thread(target=restart_thread, daemon=True).start()

    def setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(QIcon.fromTheme('applications-games'), self)
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.rebuild_tray()

    def rebuild_tray(self):
        if not hasattr(self, 'tray'):
            return
        menu = QMenu()
        act_open = menu.addAction('打开管理器')
        act_open.triggered.connect(self.showNormal)

        menu.addSeparator()
        summon_menu = menu.addMenu('快速召唤角色')
        for n in self.names:
            act = summon_menu.addAction(n)
            act.triggered.connect(lambda checked=False, name=n: self.run_async(['mascot', 'summon', name], f'已召唤 {name}'))

        menu.addSeparator()
        act_restore = menu.addAction('恢复被抓取的窗口')
        act_restore.triggered.connect(self.restore_windows)

        act_dismiss_one = menu.addAction('点选并删除一只')
        act_dismiss_one.triggered.connect(lambda: self.run_async(['mascot', 'dismiss', '-s'], '正在等待屏幕点选以消除桌宠...'))

        act_dismiss_same = menu.addAction('点选并删除同类')
        act_dismiss_same.triggered.connect(lambda: self.run_async(['mascot', 'dismiss', '-fs', '-s'], '正在等待屏幕点选以消除同类...'))

        act_dismiss_all = menu.addAction('删除全部桌宠')
        act_dismiss_all.triggered.connect(self.dismiss_all)

        menu.addSeparator()
        act_restart = menu.addAction('重启桌宠服务')
        act_restart.triggered.connect(self.restart_daemon)

        act_stop = menu.addAction('停止桌宠服务')
        act_stop.triggered.connect(self.stop_daemon)

        menu.addSeparator()
        act_settings = menu.addAction('设置...')
        act_settings.triggered.connect(lambda: SettingsDialog(self).exec())

        act_quit = menu.addAction('退出前端')
        act_quit.triggered.connect(QApplication.instance().quit)

        self.tray.setContextMenu(menu)

    def closeEvent(self, event):
        if hasattr(self, 'tray') and self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            super().closeEvent(event)


def main():
    if not shutil.which('shimejictl'):
        print('错误: 未在 PATH 中找到 shimejictl 可执行文件！', file=sys.stderr)
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
