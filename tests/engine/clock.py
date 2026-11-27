"""Shared game clock. Legacy minute timestamps remain unchanged in storage."""
from campaign import campaign_minute, clock_label, close_overdue_events
from engine.effects import advance_effects


def advance_action(state, seconds):
    """Advance a completed mechanical action, including sub-minute combat rounds."""
    if type(seconds) is not int or seconds < 0:
        raise ValueError('Некорректная длительность действия.')
    if not seconds:
        return state
    start = campaign_minute(state)
    if start is None:
        start = 480
    remainder = state.get('world_second', 0)
    remainder = remainder if type(remainder) is int and 0 <= remainder < 60 else 0
    elapsed, remainder = divmod(remainder + seconds, 60)
    minute = start + elapsed
    state.update(world_minute=minute, world_second=remainder)
    state['scene'] = {**(state.get('scene') or {}), 'time': clock_label(minute)}
    state['effect_log'] = advance_effects(state, minute)
    if isinstance(state.get('agenda'), list):
        close_overdue_events(state['agenda'], minute)
    return state
