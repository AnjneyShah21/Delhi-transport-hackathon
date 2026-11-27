"""Small, local parser that turns common trip requests into planner constraints."""
from __future__ import annotations

import re


def _clock(text: str) -> str | None:
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b', text, re.I)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    marker = (match.group(3) or '').replace('.', '').lower()
    if hour > 23 or minute > 59:
        return None
    if marker:
        if hour < 1 or hour > 12:
            return None
        hour = hour % 12 + (12 if marker == 'pm' else 0)
    return f'{hour:02d}:{minute:02d}'


def parse_request(text: str, default_from: str = '', default_to: str = '', default_time: str = '08:30') -> dict:
    raw = ' '.join((text or '').strip().split())
    lower = raw.casefold()
    origin, destination = default_from, default_to

    origin_match = re.search(r'\b(?:from|starting at|start at|leaving)\s+(.+?)(?=\s+(?:to|before|by|at|and|while|avoid|with)\b|[,!?]|$)', raw, re.I)
    if origin_match:
        origin = origin_match.group(1).strip(' .!?')

    destination_pattern = r'\b(?:get me to|take me to|reach|reaching|arrive at|going to|destination(?: is)?|route to)\s+(.+?)(?=\s+(?:before|by|around|while|from|and avoid|avoiding|with only|using)\b|[,!?]|$)'
    destination_match = re.search(destination_pattern, raw, re.I)
    if destination_match:
        destination = destination_match.group(1).strip(' .!?')
    else:
        simple_to = re.search(r'\bto\s+([A-Z][\w .\-]+?)(?=\s+(?:before|by|around|avoid|with|using)\b|[,!?]|$)', raw)
        if simple_to:
            destination = simple_to.group(1).strip(' .!?')

    arrive_by = None
    deadline_match = re.search(r'\b(?:before|by|reach\s+by|arrive\s+by)\s+(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)', raw, re.I)
    if deadline_match:
        arrive_by = _clock(deadline_match.group(1))

    departure = default_time
    leave_match = re.search(r'\b(?:leave|depart|start|travel)\s+(?:at|around|by)?\s*(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?)', raw, re.I)
    if leave_match:
        departure = _clock(leave_match.group(1)) or departure

    max_transfers = -1
    if re.search(r'\b(?:only\s+one\s+bus|one\s+bus|direct|no\s+transfers?|without\s+changing)\b', lower):
        max_transfers = 0
    elif re.search(r'\b(?:one\s+transfer|at\s+most\s+one\s+transfer|up\s+to\s+one\s+transfer|two\s+buses)\b', lower):
        max_transfers = 1

    walking = bool(re.search(r'\b(?:minimum|minimise|minimize|less|shorter|avoid)\s+walking\b|\bminimal walking\b', lower))
    avoid_crowds = bool(re.search(r'\b(?:avoid|less|lower|minimum|minimise|minimize|quiet|quieter)\s+(?:the\s+)?crowd(?:ed|ing)?\b|\buncrowded\b', lower))
    profile = 'standard'
    comfort = 50
    sort = 'balanced'
    if walking:
        profile = 'older_adult'
        max_transfers = 0 if max_transfers < 0 else max_transfers
    if avoid_crowds:
        profile = 'comfort'
        comfort = 85
        sort = 'crowd'
    if re.search(r'\bwheelchair\b|\bstep[- ]free\b', lower):
        profile = 'wheelchair'
    elif re.search(r'\b(?:woman|women|female|alone|safety|safe)\b', lower):
        profile = 'women_safety'
    elif re.search(r'\b(?:child|children|kids|family|stroller)\b', lower):
        profile = 'family'
    elif re.search(r'\b(?:elderly|older adult|senior|limited walking)\b', lower):
        profile = 'older_adult'

    understood = []
    if origin_match: understood.append('origin')
    if destination_match or (not destination_match and simple_to if 'simple_to' in locals() else False): understood.append('destination')
    if arrive_by: understood.append('arrival deadline')
    if departure != default_time: understood.append('departure time')
    if max_transfers == 0: understood.append('direct bus only')
    elif max_transfers == 1: understood.append('at most one transfer')
    if walking: understood.append('minimum walking requested')
    if avoid_crowds: understood.append('avoid crowded routes')

    notices = []
    if walking:
        notices.append('Walking paths and distance are not connected; planner favors direct routes as a proxy.')
    if profile == 'women_safety':
        notices.append('Verified safety data is unavailable; routes are not ranked as safer.')
    return {
        'origin': origin, 'destination': destination, 'time': departure,
        'arrive_by': arrive_by or '', 'max_transfers': max_transfers,
        'travel_profile': profile, 'comfort': comfort, 'sort': sort,
        'walking_minimized': walking, 'avoid_crowds': avoid_crowds,
        'understood': understood, 'notices': notices,
        'parser': 'local rule-based intent parser',
    }
