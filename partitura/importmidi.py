#!/usr/bin/env python
import argparse
import numpy as np
from collections import defaultdict
import warnings
import logging
from scipy.interpolate import interp1d

import mido

import partitura.score as score
from partitura import save_musicxml
from partitura.utils import partition
from partitura.musicanalysis import estimate_spelling, estimate_key, estimate_voices

__all__ = ['load_midi']
LOGGER = logging.getLogger(__name__)


def load_midi(fn, part_voice_assign_mode=0, ensure_list=False, quantization_unit=None):

    """Load a musical score from a MIDI file.

    Available options for part and voice assignment:

    0: return one Part per track, with voices assigned by channel
    1. return one PartGroup per track, with Parts assigned by channel (no voices)
    2. return single Part with voices assigned by track (tracks are combined, channel info is ignored)
    3. return one Part per track, without voices (channel info is ignored)
    4. return single Part without voices (channel and track info is ignored)
    5. return one Part per <track, channel> combination, without voices

    Parameters
    ----------
    fn : type
         Description of `fn`
    channels : str, optional
        Target of channel information. Possible values: {'parts', 'voices',
        None}. When channels='parts' each voice in a track will be assigned
        to a separate Part. The notes on the same channel occuring in
        different tracks will be assigned to different Part objects, not a
        single one. Defaults to 'voices'
    tracks : {'parts', 'voices', None}, optional
         Description of `tracks`. Possible values: {'parts', 'voices',
        None}, Defaults to 'parts'
    ensure_list : bool, optional
         Description of `tracks`. Defaults to False
    quantization_unit : integer or None, optional
         If not None, quantize MIDI times to multiples of this unit.  .
        Defaults to None.

    Returns
    -------
    type
        Description of return value
    
    """
    mid = mido.MidiFile(fn)
    divs = mid.ticks_per_beat

    # as key for the dict use channel * 128 (max number of pitches) + pitch
    def note_hash(channel, pitch):
        """Generate a note hash."""
        return channel * 128 + pitch

    # these lists will contain information from dedicated tracks for meta
    # information (i.e. without notes)
    global_time_sigs = []
    global_key_sigs = []
    global_tempos = []

    # these dictionaries will contain meta information indexed by track (only
    # for tracks that contain notes)
    time_sigs_by_track = {}
    key_sigs_by_track = {}
    tempos_by_track = {}
    track_names_by_track = {}
    # notes are indexed by (track, channel) tuples
    notes_by_track_ch = {}

    for track_nr, track in enumerate(mid.tracks):
        time_sigs = []
        key_sigs = []
        # tempos = []
        notes = defaultdict(list)
        # dictionary for storing the last onset time and velocity for each
        # individual note (i.e. same pitch and channel)
        sounding_notes = {}
        # current time (will be updated by delta times in messages)
        t = 0

        for msg in track:

            # print(msg)

            t += msg.time

            if quantization_unit is not None:
                t = quantize(t, quantization_unit)
            
            if msg.is_meta:
                if msg.type == 'time_signature':
                    time_sigs.append((t, msg.numerator, msg.denominator))
                if msg.type == 'key_signature':
                    key_sigs.append((t, msg.key))
                if msg.type == 'set_tempo':
                    global_tempos.append((t, 60*10**6/msg.tempo))

            note_on = msg.type == 'note_on'
            note_off = msg.type == 'note_off'

            if not (note_on or note_off):
                continue

            # hash sounding note
            note = note_hash(msg.channel, msg.note)

            # start note if it's a 'note on' event with velocity > 0
            if note_on and msg.velocity > 0:

                # save the onset time and velocity
                sounding_notes[note] = (t, msg.velocity)

            # end note if it's a 'note off' event or 'note on' with velocity 0
            elif note_off or (note_on and msg.velocity == 0):

                if note not in sounding_notes:
                    warnings.warn('ignoring MIDI message %s' % msg)
                    continue

                # append the note to the list associated with the channel
                notes[msg.channel].append((sounding_notes[note][0], msg.note, t-sounding_notes[note][0]))
                                          # sounding_notes[note][1]])
                # remove hash from dict
                del sounding_notes[note]

        # if a track has no notes, we assume it may contain global time/key sigs
        # and tempo values
        if not notes:
            global_time_sigs.extend(time_sigs)
            global_key_sigs.extend(key_sigs)
            # global_tempos.extend(tempos)

        for ch, ch_notes in notes.items():
            # if there are any notes, store the notes along with key sig / time
            # sig / tempo information under the key (track_nr, ch_nr)
            if len(ch_notes) > 0:
                notes_by_track_ch[(track_nr, ch)] = ch_notes
                time_sigs_by_track[track_nr] = time_sigs
                key_sigs_by_track[track_nr] = key_sigs
                # tempos_by_track[track_nr] = tempos
                track_names_by_track[track_nr] = track.name

    tr_ch_keys = sorted(notes_by_track_ch.keys())
    group_part_voice_keys = assign_group_part_voice(part_voice_assign_mode, tr_ch_keys)

    # pairs of (part, voice) for each note
    part_voice_list = [(part, voice) for tr_ch, (_, part, voice)
                       in zip(tr_ch_keys, group_part_voice_keys)
                       for i in range(len(notes_by_track_ch[tr_ch]))]

    # pitch spelling, voice estimation and key estimation are done on a
    # structured array (onset, pitch, duration) of all notes in the piece
    # jointly, so we concatenate all notes
    # note_list = sorted(note for notes in (notes_by_track_ch[key] for key in tr_ch_keys) for note in notes)
    note_list = [note for notes in (notes_by_track_ch[key] for key in tr_ch_keys) for note in notes]
    note_array = np.array(note_list, dtype=[('onset', np.int), ('pitch', np.int), ('duration', np.int)])

    if not timings_ok(note_array['duration'], divs, threshold=.1):
        return []

    LOGGER.info('pitch_spelling')
    # do pitch spelling
    step, alter, octave = estimate_spelling(note_array)
    # convert spelling to struct array, this should be inside estimate_spelling
    spelling_global = np.empty(len(step), dtype=[('step', 'U1'), ('alter', np.int), ('octave', np.int)])
    spelling_global['step'] = step
    spelling_global['alter'] = alter
    spelling_global['octave'] = octave

    by_part = defaultdict(list)
    for (part, voice), note, spelling in zip(part_voice_list, note_list, spelling_global):
        by_part[part].append((note, voice, spelling))

    parts = []
    for i, (part, note_info) in enumerate(by_part.items()):
        notes, voices, spellings = zip(*note_info)
        parts.append(create_part(divs, notes, spellings, voices,
                                 global_time_sigs, # time_sigs_by_track[track] or global_time_sigs,
                                 global_key_sigs, # key_sigs_by_track[track] or global_key_sigs,
                                 part_id='P{}'.format(i+1),
                                 part_name='P{}'.format(i+1)))

    # add tempos to first part
    part = next(score.iter_parts(parts))
    for t, qpm in global_tempos:
        part.add(t, score.Tempo(qpm, unit='q'))
        
    return parts


def timings_ok(durations, divs, threshold=.1):
    n_without_dur = sum(1 for dur in durations if not score.estimate_symbolic_duration(dur, divs))
    prop_without_dur = n_without_dur/max(1, len(durations))
    if prop_without_dur > threshold:
        # warnings.warn('{:.1f}% of the notes have irregular durations. Maybe you want to load this file as a performance rather than a score. If you do wish to interpret the MIDI as a score use the option --force-duration-analysis, but beware that analysis may be very slow and still fail. Another option is to quantize note onset and offset times by setting the `quantization_unit` keyword argument of `load_midi`) to an appropriate value'.format(100*prop_without_dur))
        LOGGER.warning('{:.1f}% of the notes ({}/{}) have irregular durations. Maybe you want to load this file as a performance rather than a score. If you do wish to interpret the MIDI as a score use the option --force-duration-analysis, but beware that analysis may be very slow and still fail. Another option is to quantize note onset and offset times by setting the `quantization_unit` keyword argument of `load_midi`) to an appropriate value'.format(100*prop_without_dur, n_without_dur, len(durations)))
    return prop_without_dur < threshold


def assign_group_part_voice(mode, track_ch_combis):
    """
    0: return one Part per track, with voices assigned by channel
    1. return one PartGroup per track, with Parts assigned by channel (no voices)
    2. return single Part with voices assigned by track (tracks are combined, channel info is ignored)
    3. return one Part per track, without voices (channel info is ignored)
    4. return single Part without voices (channel and track info is ignored)
    5. return one Part per <track, channel> combination, without voices
    """
    part_group = {}
    part = {}
    voice = {}
    part_helper = {}
    voice_helper = {}
    part_group_helper = {}

    for tr, ch in track_ch_combis:
        if mode == 0:
            prt = part_helper.setdefault(tr, len(part_helper))
            vc1 = voice_helper.setdefault(tr, {})
            vc2 = vc1.setdefault(ch, len(vc1))
            part[(tr, ch)] = prt
            voice[(tr, ch)] = vc2
        elif mode == 1:
            pg = part_group_helper.setdefault(tr, len(part_group_helper))
            prt = part_helper.setdefault(ch, len(part_helper))
            part_group.setdefault((tr, ch), pg)
            part[(tr, ch)] = prt
        elif mode == 2:
            vc = voice_helper.setdefault(tr, len(voice_helper))
            part.setdefault((tr, ch), 0)
            voice[(tr, ch)] = vc
        elif mode == 3:
            prt = part_helper.setdefault(tr, len(part_helper))
            part[(tr, ch)] = prt
        elif mode == 4:
            part.setdefault((tr, ch), 0)
        elif mode == 5:
            part.setdefault((tr, ch), len(part))

    return [(part_group.get(tr_ch), part.get(tr_ch), voice.get(tr_ch))
            for tr_ch in track_ch_combis]


def estimate_clef(pitches):
    # avg_pitch = np.mean(pitches)
    center = np.median(pitches)
    # number, sign, line, octave_change):
    clefs = [score.Clef(1, 'F', 4, 0), score.Clef(1, 'G', 2, 0)]
    f = interp1d([0, 49, 70, 127], [0, 0, 1, 1], kind='nearest')
    return clefs[int(f(center))]


def create_part(ticks, notes, spellings, voices, time_sigs, key_sigs, part_id=None, part_name=None):
    LOGGER.info('create_part')

    clef = estimate_clef([pitch for _, pitch, _ in notes])
    part = score.Part(part_id)
    part.add(0, score.Divisions(ticks))
    part.add(0, clef)

    # TODO: insert key sigs
    # for t, name in key_sigs:
    #     fifths, mode = interpret_key_name(name)
    #     part.add(t, score.KeySignature(...))
    LOGGER.info('add notes')

    for (onset, pitch, duration), (step, alter, octave), voice in zip(notes, spellings, voices):
        note = score.Note(step, alter, octave, voice=int(voice or 0),
                          symbolic_duration=score.estimate_symbolic_duration(duration, ticks))
        part.add(onset, note, onset+duration)

    if not time_sigs:
        warnings.warn('No time signatures found, assuming 4/4')
        time_sigs = [(0, 4, 4)]

    time_sigs = np.array(time_sigs, dtype=np.int)

    # for convenience we add the end times for each time signature
    ts_end_times = np.r_[time_sigs[1:, 0], np.iinfo(np.int).max]
    time_sigs = np.column_stack((time_sigs, ts_end_times))
    
    LOGGER.info('add measures + time sigs')
    measure_counter = 1
    # we call item() on numpy numbers to get the value in the equivalent python type
    for ts_start, num, den, ts_end in time_sigs:
        time_sig = score.TimeSignature(num.item(), den.item())

        part.add(ts_start.item(), time_sig)
        
        measure_duration = (num.item() * ticks * 4) // den.item()
        measure_start_limit = min(ts_end.item(), part.timeline.last_point.t)

        for m_start in range(ts_start, measure_start_limit, measure_duration):
            measure = score.Measure(number=measure_counter)
            m_end = min(m_start+measure_duration, ts_end)

            part.add(m_start, measure, m_end)
            measure_counter += 1

        if np.isinf(ts_end):
            ts_end = m_end
        
    LOGGER.info('tie notes')
    # tie notes where necessary (across measure boundaries, and within measures
    # notes with compound duration)
    tie_notes(part)

    LOGGER.info('find tuplets')
    # apply simplistic tuplet finding heuristic
    find_tuplets(part)
    
    LOGGER.info('done create_part')
    return part


def find_tuplets(part):
    # quick shot at finding tuplets intended to cover some common cases.

    # are tuplets always in the same voice?

    # quite arbitrary:
    search_for_tuplets = [9, 7, 5, 3]
    # only look for x:2 tuplets
    normal_notes = 2
    
    notes = part.list_all(score.Note)
    divs_map = part.divisions_map

    candidates = []
    prev_end = None

    # 1. group consecutive notes without symbolic_duration
    for note in notes:
        if note.symbolic_duration is None:
            if note.start.t == prev_end:
                candidates[-1].append(note)
            else:
                candidates.append([note])
            prev_end = note.end.t

    # 2. within each group
    for group in candidates:
        # 3. search for the predefined list of tuplets
        for tuplet in search_for_tuplets:

            if tuplet > len(group):
                # tuplet requires more notes than we have
                continue

            durs = set(n.duration for n in group[:tuplet-1])
            if len(durs) > 1:
                # notes have different durations (possibly valid but not
                # supported here)
                continue

            start = group[0].start.t
            end = group[tuplet-1].end.t
            total_dur = end - start

            # total duration of tuplet notes must be integer-divisble by normal_notes
            if total_dur % normal_notes > 0:
                continue

            # estimate duration type
            dur_type = score.estimate_symbolic_duration(total_dur//normal_notes, int(divs_map(start)))

            if dur_type and dur_type.get('dots', 0) == 0:
                # recognized duration without dots
                dur_type['actual_notes'] = tuplet
                dur_type['normal_notes'] = normal_notes
                for note in group[:tuplet]:
                    note.symbolic_duration = dur_type


def tie_notes(part, force_duration_analysis=False):
    # split and tie notes at measure boundaries
    notes = part.list_all(score.Note)
    divs = next(iter(part.timeline.first_point.get_next_of_type(score.Divisions, eq=True)), None)
    if divs:
        divs = divs.divs
    for note in notes:
        next_measure = next(iter(note.start.get_next_of_type(score.Measure)), None)
        note_end = note.end
        while next_measure and note.end > next_measure.start:
            part.timeline.remove_ending_object(note)
            part.timeline.add_ending_object(next_measure.start.t, note)
            sym_dur = score.estimate_symbolic_duration(note_end.t-next_measure.start.t, divs)
            tie_next = score.Note(note.step, note.alter, note.octave, voice=note.voice, staff=note.staff,
                                  symbolic_duration=sym_dur)
            part.add(next_measure.start.t, tie_next, note_end.t)
            # part.timeline.add_ending_object(note_end.t, tie_next)
            note.tie_next = tie_next
            tie_next.tie_prev = note
            note = tie_next
            next_measure = next(iter(note.start.get_next_of_type(score.Measure)), None)
    # then split/tie any notes that do not have a fractional/dot duration
    divs_map = part.divisions_map
    notes = part.list_all(score.Note)
    n_without_dur = sum(1 for note in notes if note.symbolic_duration is None)
    prop_without_dur = n_without_dur/max(0, len(notes))
    no_dur_max = .1
    if not force_duration_analysis and prop_without_dur > no_dur_max:
        # warnings.warn('{:.1f}% of the notes have irregular durations. Maybe you want to load this file as a performance rather than a score. If you do wish to interpret the MIDI as a score use the option --force-duration-analysis, but beware that analysis may be very slow and still fail. Another option is to quantize note onset and offset times by setting the `quantization_unit` keyword argument of `load_midi`) to an appropriate value'.format(100*prop_without_dur))
        LOGGER.warning('{:.1f}% of the notes have irregular durations. Maybe you want to load this file as a performance rather than a score. If you do wish to interpret the MIDI as a score use the option --force-duration-analysis, but beware that analysis may be very slow and still fail. Another option is to quantize note onset and offset times by setting the `quantization_unit` keyword argument of `load_midi`) to an appropriate value'.format(100*prop_without_dur))
        return None
    

    max_splits = 2
    failed = 0
    succeeded = 0
    for i, note in enumerate(notes):
        if note.symbolic_duration is None:
            splits = score.find_tie_split_search(note.start.t, note.end.t, int(divs_map(note.start.t)), max_splits)

            if splits:
                succeeded +=1
                split_note(part, note, splits)
            else:
                failed += 1
    # print(failed, succeeded, failed/succeeded)

def split_note(part, note, splits):
    # TODO: we shouldn't do this, but for now it's a good sanity check
    assert len(splits) > 0
    # TODO: we shouldn't do this, but for now it's a good sanity check
    assert note.symbolic_duration is None
    part.remove(note)

    cur_note = note
    start, end, sym_dur = splits.pop(0)
    part.add(start, cur_note, end)

    while splits:
        next_note = score.Note(note.step, note.alter, note.octave, voice=note.voice, staff=note.staff)
        cur_note.tie_next = next_note
        next_note.tie_prev = cur_note

        cur_note = next_note
        start, end, sym_dur = splits.pop(0)
        part.add(start, cur_note, end)


def quantize(v, unit):
    """Quantize value `v` to a multiple of `unit`. When `unit` is an integer,
    the return value will be integer as well, otherwise the function will
    return a float.

    Parameters
    ----------
    v : ndarray or number
        Number to be quantized
    unit : number
        The quantization unit

    Returns
    -------
    number
        The quantized number

    Examples
    --------
    >>> quantize(13.3, 4)
    12
    >>> quantize(3.3, .5)
    3.5
    
    """
    
    r = unit * np.round(v / unit)
    if isinstance(unit, int):
        return int(r)
    else:
        return r


if __name__ == '__main__':
    import doctest
    doctest.testmod()

