"""
	Flowblade Movie Editor is a nonlinear video editor.
    Copyright 2012 Janne Liljeblad.

	This file is part of Flowblade Movie Editor <http://code.google.com/p/flowblade>.

	Flowblade Movie Editor is free software: you can redistribute it and/or modify
	it under the terms of the GNU General Public License as published by
	the Free Software Foundation, either version 3 of the License, or
	(at your option) any later version.

	Flowblade Movie Editor is distributed in the hope that it will be useful,
	but WITHOUT ANY WARRANTY; without even the implied warranty of
	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
	GNU General Public License for more details.

	You should have received a copy of the GNU General Public License
	along with Flowblade Movie Editor.  If not, see <http://www.gnu.org/licenses/>.
"""

"""
Module creates EditAction objects that have user input as input
and sequence state changes as output.

Edits, undos and redos are done by creating and calling methods on these 
EditAction objects and placing them on the undo/redo stack.
"""
import copy
import time
import thread
import sys
import traceback

import audiowaveform
import appconsts
import compositeeditor
from editorstate import current_sequence
from editorstate import get_track
from editorstate import PLAYER
import mltfilters
import movemodes
import projectdata
import resync
import undo
import updater
import utils

# GUI updates are turned off for example when doing resync action
do_gui_update = False
# HACK. Used to hold references to all created compositors as some compositors 
# crash program if garbage collected.
old_compositors = []

# ---------------------------------- atomic edit ops
def append_clip(track, clip, clip_in, clip_out):
    """
    Affects MLT c-struct and python obj values.
    """
    clip.clip_in = clip_in
    clip.clip_out = clip_out
    track.clips.append(clip) # py
    track.append(clip, clip_in, clip_out) # mlt
    resync.clip_added_to_timeline(clip, track)
    
def _insert_clip(track, clip, index, clip_in, clip_out):
    """
    Affects MLT c-struct and python obj values.
    """
    clip.clip_in = clip_in
    clip.clip_out = clip_out
    track.clips.insert(index, clip) # py
    track.insert(clip, index, clip_in, clip_out) # mlt
    resync.clip_added_to_timeline(clip, track)

def _insert_blank(track, index, length):
    track.insert_blank(index, length - 1) # -1 MLT API says so
    blank_clip = track.get_clip(index)
    current_sequence().add_clip_attr(blank_clip)
    blank_clip.clip_in = 0
    blank_clip.clip_out = length - 1 # -1, end inclusive
    blank_clip.is_blanck_clip = True
    track.clips.insert(index, blank_clip)
    
def _remove_clip(track, index):
    """
    Affects MLT c-struct and python obj values.
    """
    track.remove(index)
    clip = track.clips.pop(index)
    updater.clip_removed_during_edit(clip)
    resync.clip_removed_from_timeline(clip)
    
    return clip

# -------------------------------- combined edit ops
def _cut(track, index, clip_cut_frame, clip, clip_copy):
    """
    Does cut by removing clip and adding it and copy back
    """
    _remove_clip(track, index)
    second_out = clip.clip_out # save before insert
    _insert_clip(track, clip, index, clip.clip_in, clip_cut_frame - 1)
    _insert_clip(track, clip_copy, index + 1, clip_cut_frame, second_out)

def _cut_blank(track, index, clip_cut_frame, clip):
    """
    Cuts a blank clip in two.
    """
    _remove_clip(track, index)
    
    clip_one_length = clip_cut_frame
    clip_two_length = clip.clip_out - clip_cut_frame + 1 # +1 == cut frame part of this clip

    track.insert_blank(index, clip_one_length - 1) # -1 MLT api says so 
    track.insert_blank(index + 1, clip_two_length - 1) # -1 MLT api says so 
    
    _add_blank_to_py(track, index, clip_one_length)
    _add_blank_to_py(track, index + 1, clip_two_length)

def _add_blank_to_py(track, index, length):
    """
    Adds clip data to python side structures for clip that
    already exists in MLT data structures
    """
    blank_clip = track.get_clip(index)
    current_sequence().add_clip_attr(blank_clip)
    blank_clip.clip_in = 0
    blank_clip.clip_out = length - 1 # -1, end inclusive
    blank_clip.is_blanck_clip = True
    track.clips.insert(index, blank_clip)
    return blank_clip

# --------------------------------- util methods
def _set_in_out(clip, c_in, c_out):
    """
    Affects MLT c-struct and python obj values.
    """
    clip.clip_in = c_in
    clip.clip_out = c_out
    clip.set_in_and_out(c_in, c_out)
    
def _clip_length(clip): # check if can be removed
    return clip.clip_out - clip.clip_in + 1 # +1, end inclusive

def _frame_on_cut(clip, clip_frame):
    if clip_frame == clip.clip_in:
        return True
    if clip_frame == clip.clip_out + 1: # + 1 out is inclusive
        return True
        
    return False

def _remove_trailing_blanks(track):
    clip = track.get_clip(track.count() - 1)
    try: # trying his on empty track fails 
        while(clip.is_blank()):
            _remove_clip(track, track.count() - 1)
            clip = track.get_clip(track.count() - 1)
    except:
        pass

def _create_clip_clone(clip):
    if clip.media_type != appconsts.PATTERN_PRODUCER:
        new_clip = current_sequence().create_file_producer_clip(clip.path)
    else:
        new_clip = current_sequence().create_pattern_producer(clip.create_data)
    new_clip.name = clip.name
    return new_clip

def _create_mute_volume_filter(seq):    
    mute_filter = seq.create_filter(mltfilters.get_volume_filters_info())
    mute_filter.mlt_filter.set("gain","0")
    mute_filter.mlt_filter.set("end","0")
    return mute_filter

def _do_clip_mute(clip, volume_filter):
    clip.attach(volume_filter.mlt_filter)
    clip.mute_filter = volume_filter

def _do_clip_unmute(clip):
    clip.detach(clip.mute_filter.mlt_filter)
    clip.mute_filter = None

def _remove_consecutive_blanks(track, index):
    # WHAT IF TRACK ENDS WITH BLANKS??
    # THIS MAY NOT BE ATTEMPTED IN SUCH A CASE?
    lengths = []
    while track.clips[index].is_blanck_clip:
        lengths.append(track.clips[index].clip_length())
        _remove_clip(track, index)
    return lengths
        
#---------------------------------------------- EDIT ACTION
class EditAction:
    """
    Packages together edit data and methods to make an undoable 
    change to sequence.
    
    data - input is dict with named attributes that correspond
    to usage in undo_func and redo_func
    
    redo_func is written so that it can be called also when edit is first done
    and do_edit() is called.
    """
    def __init__(self, undo_func, redo_func, data):
        # Functions that change state both ways.
        self.undo_func = undo_func
        self.redo_func = redo_func
        
        # Grabs data as object members.
        self.__dict__.update(data)
        
    def do_edit(self):
        self.redo()
        undo.register_edit(self)

    def undo(self):
        PLAYER().stop_playback()
        movemodes.clear_selected_clips()  # selection very likely not valid soon after change in sequence

        self.undo_func(self)

        resync.calculate_and_set_child_clip_sync_states()
    
        if do_gui_update:
            self._update_gui()
            
    def redo(self):
        PLAYER().stop_playback()
        movemodes.clear_selected_clips() # selection very likely not valid soon after change in sequence

        self.redo_func(self)
        
        resync.calculate_and_set_child_clip_sync_states()
            
        if do_gui_update:
            self._update_gui()
        
    def _update_gui(self):
        updater.update_tline_scrollbar() # Slider needs adjust to possily new program length.
                                         # This REPAINTS TIMELINE as side effect.
        updater.update_kf_editor()


# ---------------------------------------------------- SYNC DATA
class SyncData:
    """
    Captures sync between two clips, values filled at use sites.
    """
    def __init__(self):
        self.pos_offset = None
        self.clip_in = None
        self.clip_out = None
        self.master_clip = None
        self.master_inframe = None
        self.master_audio_index = None
           
#-------------------- APPEND CLIP
# "track","clip","clip_in","clip_out"
# Appends clip to track
def append_action(data):
    action = EditAction(_append_undo,_append_redo, data)
    return action

def _append_undo(self):
    self.clip = _remove_clip(self.track, len(self.track.clips) - 1)

def _append_redo(self):
    self.clip.index = self.track.count()
    append_clip(self.track, self.clip, self.clip_in, self.clip_out)


#----------------- REMOVE MULTIPLE CLIPS
# "track","from_index","to_index"
def remove_multiple_action(data):
    action = EditAction(_remove_multiple_undo,_remove_multiple_redo, data)
    return action

def _remove_multiple_undo(self):
    clips_count = self.to_index + 1 - self.from_index # + 1 == to_index inclusive
    for i in range(0, clips_count):
        add_clip = self.clips[i]
        index = self.from_index + i
        _insert_clip(self.track, add_clip, index, add_clip.clip_in, \
                     add_clip.clip_out)

def _remove_multiple_redo(self):
    self.clips = []
    for i in range(self.from_index, self.to_index + 1):
        removed_clip = _remove_clip(self.track, self.from_index)
        self.clips.append(removed_clip)


#----------------- LIFT MULTIPLE CLIPS 
# "track","from_index","to_index"
def lift_multiple_action(data):
    action = EditAction(_lift_multiple_undo,_lift_multiple_redo, data)
    action.blank_clip = None
    return action

def _lift_multiple_undo(self):
    # Remove blank
    _remove_clip(self.track, self.from_index)
    
    # Insert clips
    clips_count = self.to_index + 1 - self.from_index # + 1 == to_index inclusive
    for i in range(0, clips_count):
        add_clip = self.clips[i]
        index = self.from_index + i
        _insert_clip(self.track, add_clip, index, add_clip.clip_in, \
                     add_clip.clip_out)

def _lift_multiple_redo(self):
    # Remove clips
    self.clips = []
    removed_length = 0
    for i in range(self.from_index, self.to_index + 1): # + 1 == to_index inclusive
        removed_clip = _remove_clip(self.track, self.from_index)
        self.clips.append(removed_clip)
        removed_length += _clip_length(removed_clip)

    # Insert blank
    _insert_blank(self.track, self.from_index, removed_length)


#----------------- CUT CLIP 
# "track","clip","index","clip_cut_frame"
# Cuts clip at frame by creating two clips and setting ins and outs.
def cut_action(data):
    action = EditAction(_cut_undo,_cut_redo, data)
    return action

def _cut_undo(self):
    # Remove both and add old
    _remove_clip(self.track, self.index)
    _remove_clip(self.track, self.index)
    _insert_clip(self.track, self.clip, self.index, self.clip.clip_in, \
                 self.new_clip.clip_out)

def _cut_redo(self):
    # Create new second clip if does not exist
    if(not hasattr(self, "new_clip")):
        self.new_clip = _create_clip_clone(self.clip)
    
    _cut(self.track, self.index, self.clip_cut_frame, self.clip, \
         self.new_clip)


#----------------- INSERT CLIP
# "track","clip","index","clip_in","clip_out"
# Inserts clip at index into track
def insert_action(data):
    action = EditAction(_insert_undo,_insert_redo, data)
    return action

def _insert_undo(self):
    _remove_clip(self.track, self.index)

def _insert_redo(self):
    _insert_clip(self.track, self.clip, self.index, self.clip_in, \
                 self.clip_out)


#----------------- 3 POINT OVERWRITE
# "track","clip", "clip_in","clip_out","in_index","out_index"
def three_point_overwrite_action(data):
    action = EditAction(_three_over_undo, _three_over_redo, data)
    return action
    
def _three_over_undo(self):
    _remove_clip(self.track, self.in_index)
    
    clips_count = self.out_index + 1 - self.in_index # + 1 == to_index inclusive
    for i in range(0, clips_count):
        add_clip = self.clips[i]
        index = self.in_index + i
        _insert_clip(self.track, add_clip, index, add_clip.clip_in, \
                     add_clip.clip_out)

def _three_over_redo(self):
    # Remove and replace
    self.clips = []
    for i in range(self.in_index, self.out_index + 1): # + 1 == out_index inclusive
        removed_clip = _remove_clip(self.track, i)
        self.clips.append(removed_clip)

    _insert_clip(self.track, self.clip, self.in_index, self.clip_in, self.clip_out)

#----------------- SYNC OVERWRITE
#"track","clip","clip_in","clip_out","frame"
def sync_overwrite_action(data):
    action = EditAction(_sync_over_undo, _sync_over_redo, data)
    return action
    
def _sync_over_undo(self):
    # Remove overwrite clip
    track = self.track
    _remove_clip(track, self.in_index)
    
    # Fix in clip and remove cut created clip if in was cut
    if self.in_clip_out != -1:
        in_clip = _remove_clip(track, self.in_index - 1)
        copy_clip = _create_clip_clone(in_clip) 
        _insert_clip(track, copy_clip, self.in_index - 1,
                     in_clip.clip_in, self.in_clip_out)
        self.removed_clips.pop(0) # The end half of insert cut
    
    # Fix out clip and remove cut created clip if out was cut
    if self.out_clip_in != -1:
        try:
            out_clip = _remove_clip(track, self.out_index)
            copy_clip = _create_clip_clone(out_clip)
            if len(self.removed_clips) > 0: # If overwrite was done inside single clip 
                                            # we don' need to put end half of out clip back in 
                _insert_clip(track, copy_clip, self.out_index,
                         self.out_clip_in, out_clip.clip_out)
                self.removed_clips.pop(-1) # Front half of out clip
        except:
            pass
    
    # Put back old clips
    for i in range(0, len(self.removed_clips)):
        clip = self.removed_clips[i];
        _insert_clip(self.track, clip, self.in_index + i, clip.clip_in,
                     clip.clip_out)

def _sync_over_redo(self):
    # Cut at in point if not already on cut
    track = self.track
    in_clip_in, in_clip_out = _overwrite_cut_track(track, self.frame)
    self.in_clip_out = in_clip_out # out frame of the clip *previous* to overwritten clip after cut
    self.over_out = self.frame + self.clip_out - self.clip_in + 1 # +1 out frame incl.
    
    # If out point in track area we need to cut out point too
    if track.get_length() > self.over_out:
        out_clip_in, out_clip_out = _overwrite_cut_track(track, self.over_out)
        self.out_clip_in = out_clip_in
    else:
        self.out_clip_in = -1

    # Splice out clips in overwrite range
    self.removed_clips = []
    self.in_index = track.get_clip_index_at(self.frame)
    self.out_index = track.get_clip_index_at(self.over_out)
    for i in range(self.in_index, self.out_index):
        removed_clip = _remove_clip(track, self.in_index)
        self.removed_clips.append(removed_clip)

#------------------------------------- GAP APPEND
#"track","clip","clip_in","clip_out","frame"
def gap_append_action(data):
    action = EditAction(_gap_append_undo, _gap_append_redo, data)
    return action

def _gap_append_undo(self):
    pass

def _gap_append_redo(self):
    pass
        

#----------------- TWO_ROLL_TRIM
# "track","index","from_clip","to_clip","delta","edit_done_callback"
# "cut_frame"
def tworoll_trim_action(data):
    action = EditAction(_tworoll_trim_undo,_tworoll_trim_redo, data)
    return action

def _tworoll_trim_undo(self):
    _remove_clip(self.track, self.index)
    _remove_clip(self.track, self.index - 1)
    if self.non_edit_side_blank == False:
        _insert_clip(self.track, self.from_clip, self.index - 1, \
                     self.from_clip.clip_in, \
                     self.from_clip.clip_out - self.delta)
        _insert_clip(self.track, self.to_clip, self.index, \
                     self.to_clip.clip_in - self.delta, \
                     self.to_clip.clip_out )
    elif self.to_clip.is_blanck_clip:
        _insert_clip(self.track, self.from_clip, self.index - 1, \
                     self.from_clip.clip_in, \
                     self.from_clip.clip_out - self.delta)
        _insert_blank(self.track, self.index, self.to_length)
    else: # from clip is blank
        _insert_blank(self.track, self.index - 1, self.from_length)
        _insert_clip(self.track, self.to_clip, self.index, \
                     self.to_clip.clip_in - self.delta, \
                     self.to_clip.clip_out )

    #self.edit_done_callback(False, self.cut_frame, self.delta, self.track, self.to_side_being_edited)
    
def _tworoll_trim_redo(self):
    _remove_clip(self.track, self.index)
    _remove_clip(self.track, self.index - 1)
    if self.non_edit_side_blank == False:
        _insert_clip(self.track, self.from_clip, self.index - 1, \
                     self.from_clip.clip_in, \
                     self.from_clip.clip_out + self.delta)
        _insert_clip(self.track, self.to_clip, self.index, \
                     self.to_clip.clip_in + self.delta, \
                     self.to_clip.clip_out )
    elif self.to_clip.is_blanck_clip:
        _insert_clip(self.track, self.from_clip, self.index - 1, \
                     self.from_clip.clip_in, \
                     self.from_clip.clip_out + self.delta)
        self.to_length = self.to_clip.clip_out - self.to_clip.clip_in + 1 # + 1 out incl
        _insert_blank(self.track, self.index, self.to_length - self.delta)
    else: # from clip is blank
        self.from_length = self.from_clip.clip_out - self.from_clip.clip_in + 1  # + 1 out incl
        _insert_blank(self.track, self.index - 1, self.from_length + self.delta )
        _insert_clip(self.track, self.to_clip, self.index, \
                     self.to_clip.clip_in + self.delta, \
                     self.to_clip.clip_out )

    if self.first_do == True:
        self.first_do = False
        self.edit_done_callback(True, self.cut_frame, self.delta, self.track, self.to_side_being_edited)

#-------------------- INSERT MOVE
# "track","insert_index","selected_range_in","selected_range_out"
# "move_edit_done_func"
# Splices out clips in range and splices them in at given index
def insert_move_action(data):
    action = EditAction(_insert_move_undo,_insert_move_redo, data)
    return action

def _insert_move_undo(self):    
    # remove clips
    for i in self.clips:
        _remove_clip(self.track, self.real_insert_index)

    # insert clips
    for i in range(0, len(self.clips)):
        clip = self.clips[i]
        _insert_clip(self.track, clip, self.selected_range_in + i, \
                     clip.clip_in, clip.clip_out )

    self.move_edit_done_func(self.clips)

def _insert_move_redo(self):
    self.clips = []

    self.real_insert_index = self.insert_index
    clips_length = self.selected_range_out - self.selected_range_in + 1

    # if insert after range it is different when clips removed
    if self.real_insert_index > self.selected_range_out:
        self.real_insert_index -= clips_length
    
    # remove and save clips
    for i in range(0, clips_length):
        removed_clip = _remove_clip(self.track, self.selected_range_in)
        self.clips.append(removed_clip)
    
    # insert clips
    for i in range(0, clips_length):
        clip = self.clips[i]
        _insert_clip(self.track, clip, self.real_insert_index + i, \
                     clip.clip_in, clip.clip_out )

    self.move_edit_done_func(self.clips)

#-------------------- MULTITRACK INSERT MOVE
# "track","to_track","insert_index","selected_range_in","selected_range_out"
# "move_edit_done_func"
# Splices out clips in range and splices them in at given index
def multitrack_insert_move_action(data):
    action = EditAction(_multitrack_insert_move_undo,_multitrack_insert_move_redo, data)
    return action

def _multitrack_insert_move_undo(self):    
    # remove clips
    for i in self.clips:
        _remove_clip(self.to_track, self.insert_index)

    # insert clips
    for i in range(0, len(self.clips)):
        clip = self.clips[i]
        _insert_clip(self.track, clip, self.selected_range_in + i, \
                     clip.clip_in, clip.clip_out )

    self.move_edit_done_func(self.clips)

def _multitrack_insert_move_redo(self):
    self.clips = []

    clips_length = self.selected_range_out - self.selected_range_in + 1
    
    # remove clips
    for i in range(0, clips_length):
        removed_clip = _remove_clip(self.track, self.selected_range_in)
        self.clips.append(removed_clip)
    
    # insert clips
    for i in range(0, clips_length):
        clip = self.clips[i]
        _insert_clip(self.to_track, clip, self.insert_index + i, \
                     clip.clip_in, clip.clip_out )

    # Remove wrong sized waveforms
    audiowaveform.maybe_delete_waveforms(self.clips, self.to_track)

    self.move_edit_done_func(self.clips)
    

    
#----------------- OVERWRITE MOVE
# "track","over_in","over_out","selected_range_in"
# "selected_range_out","move_edit_done_func"
# Lifts clips from track and overwrites part of track with them
def overwrite_move_action(data):
    action = EditAction(_overwrite_move_undo, _overwrite_move_redo, data)
    return action

def _overwrite_move_undo(self):
    print "_overwrite_move_undo" 
    track = self.track
        
    # Remove moved clips
    moved_clips_count = self.selected_range_out - self.selected_range_in + 1 # + 1 == out inclusive
    moved_index = track.get_clip_index_at(self.over_in)
    for i in range(0, moved_clips_count):
        _remove_clip(track, moved_index)
        
    # Fix in clip and remove cut created clip if in was cut
    if self.in_clip_out != -1:
        in_clip = _remove_clip(track, moved_index - 1)
        if in_clip.is_blanck_clip != True:
            _insert_clip(track, in_clip, moved_index - 1,
                         in_clip.clip_in, self.in_clip_out)
        else: # blanks can't be resized, so must put in new blank
            _insert_blank(track, moved_index - 1, self.in_clip_out - in_clip.clip_in + 1)
        self.removed_clips.pop(0)
    
    # Fix out clip and remove cut created clip if out was cut
    if self.out_clip_in != -1:
        # If moved clip/s were last in the track and were moved slightly 
        # forward and were still last in track after move
        # this leaves a trailing black that has been removed and this will fail
        try:
            out_clip = _remove_clip(track, moved_index)
            if len(self.removed_clips) > 0: # If overwrite was done inside single clip everything is already in order
                #_insert_clip(track, out_clip, moved_index,
                #         self.out_clip_in, out_clip.clip_out)
                if not out_clip.is_blanck_clip:
                    _insert_clip(track, out_clip, moved_index,
                             self.out_clip_in, out_clip.clip_out)
                else: # blanks can't be resized, so must put in new blank
                    _insert_blank(track, moved_index, self.out_clip_length)
                self.removed_clips.pop(-1) 
        except:
            print "paass"
            pass
    
    # Put back old clips
    for i in range(0, len(self.removed_clips)):
        clip = self.removed_clips[i]
        _insert_clip(track, clip, moved_index + i, clip.clip_in,
                     clip.clip_out)
    
    # Remove blank from lifted clip
    # if moved clip/s were last in track, the clip were trying to remove
    # has already been removed so this will fail
    try:
        _remove_clip(track, self.selected_range_in)
    except:
        pass

    # Put back lifted clips
    for i in range(0, len(self.moved_clips)):
        clip = self.moved_clips[i];
        _insert_clip(track, clip, self.selected_range_in + i, clip.clip_in,
                     clip.clip_out)
    _remove_trailing_blanks(track)

def _overwrite_move_redo(self):
    self.moved_clips = []
    track = self.track
    
    # Lift moved clips and insert blank in their place
    for i in range(self.selected_range_in, self.selected_range_out + 1): # + 1 == out inclusive
        removed_clip = _remove_clip(track, self.selected_range_in)
        self.moved_clips.append(removed_clip)

    removed_length = self.over_out - self.over_in
    _insert_blank(track, self.selected_range_in, removed_length)

    # Find out if overwrite starts after or on track end and pad track with blanck if so.
    if self.over_in >= track.get_length():
        self.starts_after_end = True
        gap = self.over_out - track.get_length()
        _insert_blank(track, len(track.clips), gap)
    else:
        self.starts_after_end = False
    
    # Cut at in point if not already on cut
    clip_in, clip_out = _overwrite_cut_track(track, self.over_in)
    self.in_clip_out = clip_out

    # Cut at out point if not already on cut and out point inside track length
    if track.get_length() > self.over_out:
        clip_in, clip_out = _overwrite_cut_track(track, self.over_out)
        self.out_clip_in = clip_in
        self.out_clip_length = clip_out - clip_in + 1 # Cut blank can't be reconstructed with clip_in data as it is always 0 for blank, so we use this
    else:
        self.out_clip_in = -1

    # Splice out clips in overwrite range
    self.removed_clips = []
    in_index = track.get_clip_index_at(self.over_in)
    out_index = track.get_clip_index_at(self.over_out)

    for i in range(in_index, out_index):
        removed_clip = _remove_clip(track, in_index)
        self.removed_clips.append(removed_clip)

    # Insert overwrite clips
    for i in range(0, len(self.moved_clips)):
        clip = self.moved_clips[i]
        _insert_clip(track, clip, in_index + i, clip.clip_in, clip.clip_out)

    _remove_trailing_blanks(track)

def _overwrite_cut_track(track, frame):
    """
    If frame is on an existing cut, then the method does nothing and returns tuple (-1, -1) 
    to signal that no cut was made.
    
    If frame is in middle of clip or blank, then the method cuts that item in two
    and returns tuple of in and out frames of the clip that was cut as they
    were before the cut, for the purpose of having information to do undo later.
    """
    index = track.get_clip_index_at(frame)
    clip = track.clips[index]
    orig_in_out = (clip.clip_in, clip.clip_out)
    clip_out = clip.clip_out        
    clip_start_in_tline = track.clip_start(index)
    clip_frame = frame - clip_start_in_tline + clip.clip_in
    
    if not _frame_on_cut(clip, clip_frame):
        if clip.is_blank():
            add_clip = _cut_blank(track, index, clip_frame, clip)
        else:
            add_clip = _create_clip_clone(clip)
            _cut(track, index, clip_frame, clip, add_clip)
        return orig_in_out
    else:
        return (-1, -1)
        
#----------------- MULTITRACK OVERWRITE MOVE
# "track","to_track","over_in","over_out","selected_range_in"
# "selected_range_out","move_edit_done_func"
# Lifts clips from track and overwrites part of track with them
def multitrack_overwrite_move_action(data):
    action = EditAction(_multitrack_overwrite_move_undo, _multitrack_overwrite_move_redo, data)
    return action

def _multitrack_overwrite_move_undo(self):    
    track = self.track
    to_track = self.to_track

    # Remove moved clips
    moved_clips_count = self.selected_range_out - self.selected_range_in + 1 # + 1 == out inclusive
    moved_index = to_track.get_clip_index_at(self.over_in)
    for i in range(0, moved_clips_count):
        _remove_clip(to_track, moved_index)

    # Fix in clip and remove cut created clip if in was cut
    if self.in_clip_out != -1:
        in_clip = _remove_clip(to_track, moved_index - 1)
        #_insert_clip(to_track, in_clip, moved_index - 1,
        #             in_clip.clip_in, self.in_clip_out)
            
        
        if in_clip.is_blanck_clip != True:
            _insert_clip(to_track, in_clip, moved_index - 1,
                         in_clip.clip_in, self.in_clip_out)
        else: # blanks can't be resized, so must put in new blank
            _insert_blank(to_track, moved_index - 1, self.in_clip_out - in_clip.clip_in + 1)

        self.removed_clips.pop(0)

    # Fix out clip and remove cut created clip if out was cut
    if self.out_clip_in != -1:
        # If moved clip/s were last in the track and were moved slightly 
        # forward and were still last in track after move
        # this leaves a trailing black that has been removed and this will fail
        try:
            out_clip = _remove_clip(to_track, moved_index)
            if len(self.removed_clips) > 0: # If overwrite was done inside single clip everything is already in order
                if out_clip.is_blanck_clip != True:
                    _insert_clip(to_track, out_clip, moved_index,
                             self.out_clip_in, out_clip.clip_out)
                else: # blanks can't be resized, so must put in new blank
                    _insert_blank(to_track, moved_index, self.out_clip_length)

                self.removed_clips.pop(-1)
        except:
            pass

    # Put back old clips
    for i in range(0, len(self.removed_clips)):
        clip = self.removed_clips[i];
        _insert_clip(to_track, clip, moved_index + i, clip.clip_in,
                     clip.clip_out)

    # Remove blank from lifted clip
    # if moved clip/s were last in track, the clip were trying to remove
    # has already been removed so this will fail
    try:
        _remove_clip(track, self.selected_range_in)
    except:
        pass

    # Put back lifted clips
    for i in range(0, len(self.moved_clips)):
        clip = self.moved_clips[i];
        _insert_clip(track, clip, self.selected_range_in + i, clip.clip_in,
                     clip.clip_out)
                     
    _remove_trailing_blanks(track)
    _remove_trailing_blanks(to_track)

def _multitrack_overwrite_move_redo(self):
    self.moved_clips = []
    track = self.track
    to_track = self.to_track

    # Lift moved clips and insert 
    for i in range(self.selected_range_in, self.selected_range_out + 1): # + 1 == out inclusive
        removed_clip = _remove_clip(track, self.selected_range_in) # THIS LINE BUGS SOMETIMES FIND OUT WHY
        self.moved_clips.append(removed_clip)

    removed_length = self.over_out - self.over_in
    _insert_blank(track, self.selected_range_in, removed_length)

    # Find out if overwrite starts after track end and pad track with blanck if so
    if self.over_in >= to_track.get_length():
        self.starts_after_end = True
        gap = self.over_out - to_track.get_length()
        _insert_blank(to_track, len(to_track.clips), gap)
    else:
        self.starts_after_end = False
    
    # Cut at in point if not already on cut
    clip_in, clip_out = _overwrite_cut_track(to_track, self.over_in)
    self.in_clip_out = clip_out

    # Cut at out point if not already on cut
    if to_track.get_length() > self.over_out:
        clip_in, clip_out = _overwrite_cut_track(to_track, self.over_out)
        self.out_clip_in = clip_in
        self.out_clip_length = clip_out - clip_in + 1 # Cut blank can't be reconstructed with clip_in data as it is always 0 for blank, so we use this
    else:
        self.out_clip_in = -1

    # Splice out clips in overwrite range
    self.removed_clips = []
    in_index = to_track.get_clip_index_at(self.over_in)
    out_index = to_track.get_clip_index_at(self.over_out)

    for i in range(in_index, out_index):
        removed_clip = _remove_clip(to_track, in_index)
        self.removed_clips.append(removed_clip)

    # Insert overwrite clips
    for i in range(0, len(self.moved_clips)):
        clip = self.moved_clips[i]
        _insert_clip(to_track, clip, in_index + i, clip.clip_in, clip.clip_out)

    _remove_trailing_blanks(track)
    _remove_trailing_blanks(to_track)
    
    # Remove wrong sized waveforms
    audiowaveform.maybe_delete_waveforms(self.moved_clips, to_track)
    
#------------------ TRIM CLIP START
# "track","clip","index","delta","undo_done_callback"
# Trims start of clip
def trim_start_action(data): 
    action = EditAction(_trim_start_undo,_trim_start_redo, data)
    return action

def _trim_start_undo(self):
    _remove_clip(self.track, self.index)
    _insert_clip(self.track, self.clip, self.index,
                 self.clip.clip_in - self.delta, self.clip.clip_out)
    
    # Reinit one roll trim
    #self.undo_done_callback(self.track, self.index, True)


def _trim_start_redo(self):
    _remove_clip(self.track, self.index)
    _insert_clip(self.track, self.clip, self.index,
                 self.clip.clip_in + self.delta, self.clip.clip_out)

    # Reinit one roll trim 
    if self.first_do == True:
        self.first_do = False
        self.undo_done_callback(self.track, self.index, True)

#------------------ TRIM CLIP END
# "track","clip","index","delta"
# "undo_done_callback"
# Trims start of clip
def trim_end_action(data): 
    action = EditAction(_trim_end_undo,_trim_end_redo, data)
    return action

def _trim_end_undo(self):
    _remove_clip(self.track, self.index)
    _insert_clip(self.track, self.clip, self.index,
                 self.clip.clip_in, self.clip.clip_out - self.delta)
    
    # Reinit one roll trim
    # self.undo_done_callback(self.track, self.index + 1, False)

def _trim_end_redo(self):
    _remove_clip(self.track, self.index)
    _insert_clip(self.track, self.clip, self.index,
                 self.clip.clip_in, self.clip.clip_out + self.delta)

    # Reinit one roll trim
    if self.first_do == True:
        self.first_do = False
        self.undo_done_callback(self.track, self.index + 1, False)
    
#------------------- ADD FILTER
# "clip","filter_info","filter_edit_done_func"
# Adds filter to clip.
def add_filter_action(data):
    action = EditAction(_add_filter_undo,_add_filter_redo, data)
    return action

def _add_filter_undo(self):
    self.clip.detach(self.filter_object.mlt_filter)
    index = self.clip.filters.index(self.filter_object)
    self.clip.filters.pop(index)

    self.filter_edit_done_func(self.clip, len(self.clip.filters) - 1) # updates effect stack gui

def _add_filter_redo(self):
    try: # is redo, fails for first
        self.clip.attach(self.filter_object.mlt_filter)
        self.clip.filters.append(self.filter_object)
    except: # First do
        self.filter_object = current_sequence().create_filter(self.filter_info)
        self.clip.attach(self.filter_object.mlt_filter)
        self.clip.filters.append(self.filter_object)
        
    self.filter_edit_done_func(self.clip, len(self.clip.filters) - 1) # updates effect stack gui

#------------------- ADD MULTIPART FILTER
# "clip","filter_info","filter_edit_done_func"
# Adds filter to clip.
def add_multipart_filter_action(data):
    action = EditAction(_add_multipart_filter_undo,_add_multipart_filter_redo, data)
    return action

def _add_multipart_filter_undo(self):
    self.filter_object.detach_all_mlt_filters(self.clip)
    index = self.clip.filters.index(self.filter_object)
    self.clip.filters.pop(index)

    self.filter_edit_done_func(self.clip, len(self.clip.filters) - 1) # updates effect stack

def _add_multipart_filter_redo(self):
    try: # if redo, fails for first
        self.filter_object.attach_filters(self.clip)
        self.clip.filters.append(self.filter_object)
    except: # First do
        self.filter_object = current_sequence().create_multipart_filter(self.filter_info, self.clip)
        self.filter_object.attach_all_mlt_filters(self.clip)
        self.clip.filters.append(self.filter_object)
        
    self.filter_edit_done_func(self.clip, len(self.clip.filters) - 1) # updates effect stack

#------------------- REMOVE FILTER
# "clip","index,""filter_edit_done_func"
# Adds filter to clip.
def remove_filter_action(data):
    action = EditAction(_remove_filter_undo,_remove_filter_redo, data)
    return action

def _remove_filter_undo(self):
    _detach_all(self.clip)
    try:
        self.clip.filters.insert(self.index, self.filter_object)
    except:
        self.clip.filters.append(self.filter_object)

    _attach_all(self.clip)
        
    self.filter_edit_done_func(self.clip,self.index) # updates effect stack gui if needed

def _remove_filter_redo(self):
    _detach_all(self.clip)
    self.filter_object = self.clip.filters.pop(self.index)
    _attach_all(self.clip)

    self.filter_edit_done_func(self.clip, len(self.clip.filters) - 1)# updates effect stack gui

def _detach_all(clip):
    mltfilters.detach_all_filters(clip)

def _attach_all(clip):
    mltfilters.attach_all_filters(clip)

#------------------- REMOVE MULTIPLE FILTERS
# "clips"
# Adds filter to clip.
def remove_multiple_filters_action(data):
    action = EditAction(_remove_multiple_filters_undo,_remove_multiple_filters_redo, data)
    return action

def _remove_multiple_filters_undo(self):
    for clip, clip_filters in zip(self.clips, self.clip_filters):
        clip.filters = clip_filters
        _attach_all(clip)

def _remove_multiple_filters_redo(self):
    self.clip_filters = []
    for clip in self.clips:
        _detach_all(clip)
        self.clip_filters.append(clip.filters)
        clip.filters = []
        updater.clear_clip_from_editors(clip)

# -------------------------------------- CLONE FILTERS
#"clip","clone_source_clip"
def clone_filters_action(data):
    action = EditAction(_clone_filters_undo, _clone_filters_redo, data)
    return action

def _clone_filters_undo(self):
    _detach_all(self.clip)
    self.clip.filters = self.old_filters
    _attach_all(self.clip)
    
def _clone_filters_redo(self):
    if not hasattr(self, "clone_filters"):
        self.clone_filters = current_sequence().clone_filters(self.clone_source_clip)
        self.old_filters = self.clip.filters

    _detach_all(self.clip)
    self.clip.filters = self.clone_filters
    _attach_all(self.clip)

# -------------------------------------- ADD COMPOSITOR ACTION
# "origin_clip_id",in_frame","out_frame","compositor_index","a_track","b_track"
def add_compositor_action(data):
    action = EditAction(_add_compositor_undo, _add_compositor_redo, data)
    action.first_do = True
    return action

def _add_compositor_undo(self):
    current_sequence().remove_compositor(self.compositor)
    current_sequence().restack_compositors()
    
    # Hack!!! Some filters don't seem to handle setting compositors None (and the
    # following gc) and crash, so we'll hold references to them forever.
    global old_compositors
    old_compositors.append(self.compositor)
    compositeeditor.maybe_clear_editor(self.compositor)
    self.compositor = None

def _add_compositor_redo(self):    
    self.compositor = current_sequence().create_compositor(self.compositor_index)
    self.compositor.transition.set_tracks(self.a_track, self.b_track)
    self.compositor.set_in_and_out(self.in_frame, self.out_frame)
    self.compositor.origin_clip_id = self.origin_clip_id

    # Compositors are recreated continually in sequnece.restack_compositors() and cannot be identified for undo/redo using object identity 
    # so these ids must be  preserved for all succesive versions of a compositor
    if self.first_do == True:
        self.destroy_id = self.compositor.destroy_id
        self.first_do = False
    else:
        self.compositor.destroy_id = self.destroy_id

    current_sequence().add_compositor(self.compositor)
    current_sequence().restack_compositors()

    compositeeditor.set_compositor(self.compositor)

# -------------------------------------- DELETE COMPOSITOR ACTION
# "compositor"
def delete_compositor_action(data):
    action = EditAction(_delete_compositor_undo, _delete_compositor_redo, data)
    action.first_do = True
    return action

def _delete_compositor_undo(self):
    old_compositor = self.compositor 
    
    self.compositor = current_sequence().create_compositor(old_compositor.compositor_index)
    self.compositor.clone_properties(old_compositor)
    self.compositor.set_in_and_out(old_compositor.clip_in, old_compositor.clip_out)
    self.compositor.transition.set_tracks(old_compositor.transition.a_track, old_compositor.transition.b_track)

    current_sequence().add_compositor(self.compositor)
    current_sequence().restack_compositors()

    compositeeditor.set_compositor(self.compositor)

def _delete_compositor_redo(self):
    # Compositors are recreated continually in sequnece.restack_compositors() and cannot be identified for undo/redo using object identity 
    # so these ids must be  preserved for all succesive versions of a compositor.
    if self.first_do == True:
        self.destroy_id = self.compositor.destroy_id
        self.first_do = False
    else:
        self.compositor = current_sequence().get_compositor_for_destroy_id(self.destroy_id)
        
    current_sequence().remove_compositor(self.compositor)
    current_sequence().restack_compositors()
    
    # Hack!!! Some filters don't seem to handle setting compositors None (and the
    # following gc) and crash, so we'll hold references to them forever.
    global old_compositors
    old_compositors.append(self.compositor)
    
    compositeeditor.maybe_clear_editor(self.compositor)

#--------------------------------------------------- MOVE COMPOSITOR
# "compositor","clip_in","clip_out"
def move_compositor_action(data):
    action = EditAction(_move_compositor_undo, _move_compositor_redo, data)
    action.first_do = True
    return action  

def _move_compositor_undo(self):
    move_compositor = current_sequence().get_compositor_for_destroy_id(self.destroy_id)
    move_compositor.set_in_and_out(self.orig_in, self.orig_out)

    compositeeditor.set_compositor(self.compositor)

def _move_compositor_redo(self):
    # Compositors are recreated continually in sequnece.restack_compositors() and cannot be identified for undo/redo using object identity 
    # so these ids must be  preserved for all succesive versions of a compositor.
    if self.first_do == True:
        self.destroy_id = self.compositor.destroy_id
        self.orig_in = self.compositor.clip_in
        self.orig_out = self.compositor.clip_out
        self.first_do = False

    move_compositor = current_sequence().get_compositor_for_destroy_id(self.destroy_id)
    move_compositor.set_in_and_out(self.clip_in, self.clip_out)

    compositeeditor.set_compositor(self.compositor)

#----------------- AUDIO SPLICE
# "parent_clip", "audio_clip", "track"
def audio_splice_action(data):
    action = EditAction(_audio_splice_undo, _audio_splice_redo, data)
    return action

def _audio_splice_undo(self):
    to_track = self.to_track

    # Remove add audio clip
    in_index = to_track.get_clip_index_at(self.over_in)
    _remove_clip(to_track, in_index)
        
    # Fix in clip and remove cut created clip if in was cut
    if self.in_clip_out != -1:
        in_clip = _remove_clip(to_track, in_index - 1)
        _insert_clip(to_track, in_clip, in_index - 1,
                     in_clip.clip_in, self.in_clip_out)
        self.removed_clips.pop(0)

    # Fix out clip and remove cut created clip if out was cut
    if self.out_clip_in != -1:
        # If moved clip/s were last in the track and were moved slightly 
        # forward and were still last in track after move
        # this leaves a trailing black that has been removed and this will fail
        try:
            out_clip = _remove_clip(to_track, in_index)
            if len(self.removed_clips) > 0: # If overwrite was done inside single clip everything is already in order
                _insert_clip(to_track, out_clip, in_index,
                         self.out_clip_in, out_clip.clip_out)
                self.removed_clips.pop(-1) 
        except:
            pass
    
    # Put back old clips
    for i in range(0, len(self.removed_clips)):
        clip = self.removed_clips[i];
        _insert_clip(to_track, clip, in_index + i, clip.clip_in,
                     clip.clip_out)

    _do_clip_unmute(self.parent_clip)
    
    _remove_trailing_blanks(to_track)

def _audio_splice_redo(self):
    # Get shorter name for readability
    to_track = self.to_track
    
    # Find out if overwrite starts after track end and pad track with blanck if so.
    if self.over_in >= to_track.get_length():
        self.starts_after_end = True
        gap = self.over_out - to_track.get_length()
        _insert_blank(to_track, len(to_track.clips), gap)
    else:
        self.starts_after_end = False

    # Cut at in frame of overwrite range. 
    clip_in, clip_out = _overwrite_cut_track(to_track, self.over_in)
    self.in_clip_out = clip_out

    # Cut at out frame of overwrite range 
    if to_track.get_length() > self.over_out:
        clip_in, clip_out = _overwrite_cut_track(to_track, self.over_out)
        self.out_clip_in = clip_in
    else:
        self.out_clip_in = -1
    
    # Splice out clips in overwrite range
    self.removed_clips = []
    in_index = to_track.get_clip_index_at(self.over_in)
    out_index = to_track.get_clip_index_at(self.over_out)

    for i in range(in_index, out_index):
        self.removed_clips.append(_remove_clip(to_track, in_index))

    # Insert audio clip
    _insert_clip(to_track, self.audio_clip, in_index, self.parent_clip.clip_in, self.parent_clip.clip_out)

    filter = _create_mute_volume_filter(current_sequence())
    _do_clip_mute(self.parent_clip, filter)
    
    _remove_trailing_blanks(to_track)

# ------------------------------------------------- SPLIT TO SYNC
# "parent_clip","audio_clip", "over_in","over_out","to_track","from_track","parent_index"
# "to_track" == audio sync track
# "from_track" == video master track
def audio_sync_splice_action(data):
    action = EditAction(_audio_sync_splice_undo, _audio_sync_splice_redo, data)
    return action

def _audio_sync_splice_undo(self):
    _unmute_splice_parent_audio(self, self.parent_clip)

def _audio_sync_splice_redo(self):
    # Set sync data for audio clip
    sync_data = SyncData()
    sync_data.pos_offset = 0
    sync_data.clip_in = self.parent_clip.clip_in
    sync_data.clip_out = self.parent_clip.clip_out
    sync_data.master_clip = self.parent_clip

    _mute_splice_parent_audio(self, self.parent_clip)
    sync_data.master_audio_index = self.parent_audio_index

    self.audio_clip.sync_data = sync_data
    #self.to_track.sync_clips.append(self.audio_clip)

   
# ------------------------------------------------- RESYNC ALL
# No input data
def resync_all_action(data):
    action = EditAction(_resync_all_undo, _resync_all_redo, data)
    return action

def _resync_all_undo(self):
    self.actions.reverse()
    
    for action in self.actions:
        action.undo_func(action)
    
    self.actions.reverse()
        
def _resync_all_redo(self):
    if hasattr(self, "actions"):
        # Actions have already been created, this is redo
        for action in self.actions:
            action.redo_func(action)
        return

    resync_data = resync.get_resync_data_list()
    self.actions = _create_and_do_sync_actions_list(resync_data)


# ------------------------------------------------- RESYNC SOME CLIPS
# "clips"
def resync_some_clips_action(data):
    action = EditAction(_resync_some_clips_undo, _resync_some_clips_redo, data)
    return action


def _resync_some_clips_undo(self):
    self.actions.reverse()
    
    for action in self.actions:
        action.undo_func(action)
    
    self.actions.reverse()
        
def _resync_some_clips_redo(self):
    if hasattr(self, "actions"):
        # Actions have already been created, this is redo
        for action in self.actions:
            action.redo_func(action)
        return

    resync_data = resync.get_resync_data_list_for_clip_list(self.clips)
    self.actions = _create_and_do_sync_actions_list(resync_data)

def _create_and_do_sync_actions_list(resync_data_list):
    # input is list tuples list (clip, track, index, pos_off)
    actions = []
    for clip_data in resync_data_list:
        clip, track, index, pos_offset = clip_data

        # If we're in sync, do nothing
        if pos_offset == clip.sync_data.pos_offset:
            continue

        # Get new in and out frames for clip
        diff = pos_offset - clip.sync_data.pos_offset
        over_in = track.clip_start(index) - diff
        over_out = over_in + (clip.clip_out - clip.clip_in + 1)
        data = {"track":track,
                "over_in":over_in,
                "over_out":over_out,
                "selected_range_in":index,
                "selected_range_out":index,
                "move_edit_done_func":None}
        
        action = overwrite_move_action(data)
        actions.append(action)
        action.redo_func(action)

    return actions

# ------------------------------------------------- SET SYNC
# "child_index","child_track","parent_index","parent_track"
def set_sync_action(data):
    action = EditAction(_set_sync_undo, _set_sync_redo, data)
    return action
    
def _set_sync_undo(self):
    # Get clips
    child_clip = self.child_track.clips[self.child_index]
    parent_clip = self.parent_track.clips[self.parent_index]
     
    # Clear child sync data
    child_clip.sync_data = None

    # Clear resync data
    resync.clip_sync_cleared(child_clip)
    
def _set_sync_redo(self):
    # Get clips
    child_clip = self.child_track.clips[self.child_index]
    parent_clip = get_track(current_sequence().first_video_index).clips[self.parent_index]

    # Get offset
    child_clip_start = self.child_track.clip_start(self.child_index) - child_clip.clip_in
    parent_clip_start = self.parent_track.clip_start(self.parent_index) - parent_clip.clip_in
    pos_offset = child_clip_start - parent_clip_start
    
    # Set sync data
    child_clip.sync_data = SyncData()
    child_clip.sync_data.pos_offset = pos_offset
    child_clip.sync_data.master_clip = parent_clip
    child_clip.sync_data.sync_state = appconsts.SYNC_CORRECT

    resync.clip_added_to_timeline(child_clip, self.child_track)

# ------------------------------------------------- CLEAR SYNC
# "child_clip","child_track"
def clear_sync_action(data):
    action = EditAction(_clear_sync_undo, _clear_sync_redo, data)
    return action
    
def _clear_sync_undo(self):
    # Reset child sync data
    self.child_clip.sync_data = self.sync_data

    # Save data resync data for doing resyncs and sync state gui updates
    resync.clip_added_to_timeline(self.child_clip, self.child_track)

def _clear_sync_redo(self):
    # Save sync data
    self.sync_data = self.child_clip.sync_data
    
    # Clear child sync data
    self.child_clip.sync_data = None
    
    # Claer resync data
    resync.clip_sync_cleared(self.child_clip)
    
# --------------------------------------- MUTE CLIP
# "clip"
def mute_clip(data):
    action = EditAction(_mute_clip_undo,_mute_clip_redo, data)
    return action

def _mute_clip_undo(self):
    _do_clip_unmute(self.clip)

def _mute_clip_redo(self):
    mute_filter = _create_mute_volume_filter(current_sequence())
    _do_clip_mute(self.clip, mute_filter)

# --------------------------------------- UNMUTE CLIP
# "clip"
def unmute_clip(data):
    action = EditAction(_unmute_clip_undo,_unmute_clip_redo, data)
    return action

def _unmute_clip_undo(self):
    mute_filter = _create_mute_volume_filter()
    _do_clip_mute(self.clip, mute_filter)

def _unmute_clip_redo(self):
    _do_clip_unmute(self.clip)



# ----------------------------------------- TRIM END OVER BLANKS
"track","clip","clip_index"
def trim_end_over_blanks(data):
    action = EditAction(_trim_end_over_blanks_undo, _trim_end_over_blanks_redo, data)
    return action 

def _trim_end_over_blanks_undo(self):
    # put back blanks
    total_length = 0
    for i in range(0, len(self.removed_lengths)):
        length = self.removed_lengths[i]
        _insert_blank(self.track, self.clip_index + 1 + i, length)
        total_length = total_length + length

    # trim clip
    _remove_clip(self.track, self.clip_index)
    _insert_clip(self.track, self.clip, self.clip_index, self.clip.clip_in, self.clip.clip_out - total_length) 

def _trim_end_over_blanks_redo(self):
    # Remove blanks
    self.removed_lengths = _remove_consecutive_blanks(self.track, self.clip_index + 1) # +1, we're streching clip over blank are starting at NEXT index
    total_length = 0
    for length in self.removed_lengths:
        total_length = total_length + length

    # trim clip
    _remove_clip(self.track, self.clip_index)
    _insert_clip(self.track, self.clip, self.clip_index, self.clip.clip_in, self.clip.clip_out + total_length) 


# ----------------------------------------- TRIM START OVER BLANKS
# "track","clip","blank_index"
def trim_start_over_blanks(data):
    action = EditAction(_trim_start_over_blanks_undo, _trim_start_over_blanks_redo, data)
    return action

def _trim_start_over_blanks_undo(self):
    # trim clip
    _remove_clip(self.track, self.blank_index)
    _insert_clip(self.track, self.clip, self.blank_index, self.clip.clip_in + self.total_length, self.clip.clip_out)

    # put back blanks
    for i in range(0, len(self.removed_lengths)):
        length = self.removed_lengths[i]
        _insert_blank(self.track, self.blank_index + i, length)

def _trim_start_over_blanks_redo(self):
    # Remove blanks
    self.removed_lengths = _remove_consecutive_blanks(self.track, self.blank_index)
    self.total_length = 0
    for length in self.removed_lengths:
        self.total_length = self.total_length + length

    # trim clip
    _remove_clip(self.track, self.blank_index)
    _insert_clip(self.track, self.clip, self.blank_index, self.clip.clip_in - self.total_length, self.clip.clip_out) 


# ---------------------------------------- CONSOLIDATE SELECTED BLANKS
"track","index"
def consolidate_selected_blanks(data):
    action = EditAction(_consolidate_selected_blanks_undo,_consolidate_selected_blanks_redo, data)
    return action 

def _consolidate_selected_blanks_undo(self):
    _remove_clip(self.track, self.index)
    for i in range(0, len(self.removed_lengths)):
        length = self.removed_lengths[i]
        _insert_blank(self.track, self.index + i, length)

def _consolidate_selected_blanks_redo(self):
    self.removed_lengths = _remove_consecutive_blanks(self.track, self.index)
    total_length = 0
    for length in self.removed_lengths:
        total_length = total_length + length
    _insert_blank(self.track, self.index, total_length)


#----------------------------------- CONSOLIDATE ALL BLANKS
def consolidate_all_blanks(data):
    action = EditAction(_consolidate_all_blanks_undo,_consolidate_all_blanks_redo, data)
    return action     

def _consolidate_all_blanks_undo(self):
    self.consolidate_actions.reverse()
    for c_action in  self.consolidate_actions:
        track, index, removed_lengths = c_action
        _remove_clip(track, index)
        for i in range(0, len(removed_lengths)):
            length = removed_lengths[i]
            _insert_blank(track, index + i, length)
        
def _consolidate_all_blanks_redo(self):
    self.consolidate_actions = []
    for i in range(1, len(current_sequence().tracks) - 1): # -1 because hidden track
        track = current_sequence().tracks[i]
        consolidaded_indexes = []
        try_do_next = True
        while(try_do_next == True):
            if len(track.clips) == 0:
                try_do_next = False
            for i in range(0, len(track.clips)):
                if i == len(track.clips) - 1:
                    try_do_next = False
                clip = track.clips[i]
                if clip.is_blanck_clip == False:
                    continue
                try:
                    consolidaded_indexes.index(i)
                    continue
                except:
                    pass
                
                # Now consolidate from clip in index i
                consolidaded_indexes.append(i)
                removed_lengths = _remove_consecutive_blanks(track, i)
                total_length = 0
                for length in removed_lengths:
                    total_length = total_length + length
                _insert_blank(track, i, total_length)
                self.consolidate_actions.append((track, i, removed_lengths))
                break

    
    
    
    
    
        