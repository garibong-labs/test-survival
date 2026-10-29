"""User presentation settings; never part of experiment inputs or persisted time."""
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
import os
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_PROGRESS_ETA = re.compile(
    r' \| elapsed \d+:\d{2}:\d{2}; \d+/(?:\d+|\?) \w+; estimated finish (?P<eta>\S+)$'
)


class DisplayConfigError(ValueError):
    pass


@dataclass(frozen=True)
class DisplaySettings:
    timezone: tzinfo

    def timestamp(self, value, *, timespec='auto'):
        if value == 'unknown':
            return value
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if instant.tzinfo is None:
            raise ValueError('Display timestamps must include a UTC offset')
        return instant.astimezone(self.timezone).isoformat(timespec=timespec)

    def log_line(self, line):
        """Render a saved log line without modifying evidence or arbitrary message dates."""
        stamp, separator, message = line.partition(' ')
        if not separator:
            return line
        try:
            shown_stamp = self.timestamp(stamp)
        except (ValueError, OverflowError):
            return line  # Legacy/unstructured or incomplete log line.
        progress = _PROGRESS_ETA.search(message)
        if progress:
            try:
                eta = self.timestamp(progress['eta'])
            except (ValueError, OverflowError):
                pass  # Keep a malformed ETA visible without breaking status.
            else:
                message = message[:progress.start('eta')] + eta
        return shown_stamp + separator + message


UTC_DISPLAY = DisplaySettings(timezone.utc)


def settings_path():
    override = os.environ.get('XDG_CONFIG_HOME')
    base = Path(override) if override else Path.home()/'.config'
    if not base.is_absolute():
        raise DisplayConfigError('XDG_CONFIG_HOME must be an absolute directory')
    return base/'test-survival/config.toml'


def load_display_settings():
    path = settings_path()
    try:
        with path.open('rb') as stream:
            data = tomllib.load(stream)
    except FileNotFoundError:
        data = {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
        raise DisplayConfigError(f'{path}: cannot read valid TOML display settings') from None
    if set(data)-{'display'} or not isinstance(data.get('display', {}), dict):
        raise DisplayConfigError(f'{path}: only a [display] table is supported')
    display = data.get('display', {})
    if set(display)-{'timezone'}:
        raise DisplayConfigError(f'{path}: display only supports timezone')
    name = display.get('timezone', 'Asia/Seoul')
    if not isinstance(name, str) or not name:
        raise DisplayConfigError(f'{path}: display.timezone must be a nonempty IANA time zone name')
    try:
        return DisplaySettings(ZoneInfo(name))
    except (ZoneInfoNotFoundError, ValueError):
        raise DisplayConfigError(f'{path}: display.timezone is unknown or unavailable; use an installed IANA time zone name') from None
