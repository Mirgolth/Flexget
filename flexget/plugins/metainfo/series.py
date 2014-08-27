from __future__ import unicode_literals, division, absolute_import
import logging
from string import capwords
from flexget.utils.parsers.parser_common import PARSER_ANY
import re

from flexget import plugin
from flexget.event import event
from flexget.plugins.filter.series import populate_entry_fields
from flexget.utils.parsers import get_parser, ParseWarning

log = logging.getLogger('metainfo_series')


class MetainfoSeries(object):
    """
    Check if entry appears to be a series, and populate series info if so.
    """

    schema = {'type': 'boolean'}

    # Run after series plugin so we don't try to re-parse it's entries
    @plugin.priority(120)
    def on_task_metainfo(self, task, config):
        # Don't run if we are disabled
        if config is False:
            return
        for entry in task.entries:
            # If series plugin already parsed this, don't touch it.
            if entry.get('series_name'):
                continue
            self.guess_entry(entry)

    def guess_entry(self, entry, allow_seasonless=False):
        """Populates series_* fields for entries that are successfully parsed."""
        if entry.get('series_parser') and entry['series_parser'].valid:
            # Return true if we already parsed this, false if series plugin parsed it
            return entry.get('series_guessed')
        parser = self.guess_series(entry['title'], allow_seasonless=allow_seasonless, quality=entry.get('quality'))
        if parser:
            populate_entry_fields(entry, parser)
            entry['series_guessed'] = True
            return True
        return False

    def guess_series(self, title, allow_seasonless=False, quality=None):
        """Returns a valid series parser if this `title` appears to be a series"""

        parsed = get_parser().parse(title, PARSER_ANY, title, identified_by='auto', allow_seasonless=allow_seasonless)
        if parsed.valid and parsed.is_series:
            return parsed


@event('plugin.register')
def register_plugin():
    plugin.register(MetainfoSeries, 'metainfo_series', api_ver=2)
