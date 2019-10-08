"""

This file contains test functions for Matchfile import

"""

import logging
import unittest
from tempfile import TemporaryFile

from . import MATCH_IMPORT_EXPORT_TESTFILES

from partitura.match import MatchFile, parse_matchline


class TestLoadMatch(unittest.TestCase):

    def test_import(self):

        for fn in MATCH_IMPORT_EXPORT_TESTFILES:

            # read file
            fileData = [l.decode('utf8').strip() for l in open(fn, 'rb')]
            # parse match file
            match = MatchFile(fn)

            # Not mached lines are returned as False
            matched_lines = [True if ml else False for ml in match.lines]
            # Assert that all lines in the matchfile where matched
            self.assertTrue(sum(matched_lines), len(fileData))

    def test_match_lines(self):

        snote_line = 'snote(1-1,[E,n],4,0:1,0,1/4,-1.0,0.0,[staff1])'
        note_line = 'note(0,[E,n],4,471720,472397,472397,49)'
        old_note_line = 'note(0,[E,n],4,471720,472397,49)'
        snote_note_line = 'snote(1-1,[E,n],4,0:1,0,1/4,-1.0,0.0,[staff1])-note(0,[E,n],4,471720,472397,472397,49).'
        snote_deletion_line = 'snote(1-1,[E,n],4,0:1,0,1/4,-1.0,0.0,[staff1])-deletion.'
        note_insertion_line = 'insertion-' + note_line + '.'
        info_line = 'info(matchFileVersion,4.0).'
        meta_line = 'meta(keySignature,C Maj/A min,0,-1.0).'
        sustain_line = 'sustain(779,59).'

        matchlines = [snote_note_line,
                      snote_deletion_line,
                      note_insertion_line,
                      info_line,
                      meta_line,
                      sustain_line]

        for ml in matchlines:
            mo = parse_matchline(ml)
            self.assertTrue(mo.matchline, ml)


if __name__ == '__main__':

    unittest.main()
