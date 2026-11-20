import copy
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import pytest
from tracker import (BASE, ZONE, Course, VerificationError, atomic_write, build_feed,
    calculate_updates, combine, empty_state, in_window, load_state, next_monday, parse_free, parse_week)

NOW = datetime(2026, 11, 15, 10, tzinfo=ZONE)
MONDAY = date(2026, 11, 16)
FIXTURES = Path(__file__).parent / 'fixtures'

def course(key='1', status='bookable', day='2026-11-18'):
    return Course(key, day, '18:00', 'Jumping Fitness', 11, 11 if status=='full' else 4, status)

def step(state, items, minute=0, now=NOW):
    return calculate_updates(state, {c.slot_id:c for c in items}, now + timedelta(minutes=minute))

@pytest.mark.parametrize('today,expected',[
    ('2026-11-15','2026-11-16'),('2026-11-16','2026-11-23'),
    ('2026-12-31','2027-01-04'),('2026-03-29','2026-03-30'),('2026-10-25','2026-10-26')])
def test_next_calendar_week(today,expected):
    assert next_monday(date.fromisoformat(today)).isoformat()==expected

def test_only_next_week():
    for d, expected in [(15,False),(16,True),(22,True),(23,False)]:
        assert in_window(date(2026,11,d),NOW.date()) is expected

def test_existing_baseline_is_silent():
    state, event=step(empty_state(),[course()])
    state,event=step(state,[course()],15)
    assert event is None

def test_release_needs_confirmation_and_is_once_per_week():
    state,_=step(empty_state(),[])
    state,event=step(state,[course()],15)
    assert event is None
    state,event=step(state,[course()],30)
    assert event['id']=='release:2026-11-16'
    state,event=step(state,[course(),course('2')],45)
    state,event=step(state,[course(),course('2')],60)
    assert event is None and len(state['events'])==1

@pytest.mark.parametrize('baseline',[True,False])
def test_full_then_cancelled_never_notifies(baseline):
    state,_=step(empty_state(),[course(status='full')] if baseline else [])
    state,_=step(state,[course(status='full')],10)
    state,event=step(state,[course()],20)
    state,event=step(state,[course()],30)
    assert event is None

def test_first_full_observation_suppresses_even_when_second_is_free():
    state,_=step(empty_state(),[])
    state,_=step(state,[course(status='full')],1)
    state,_=step(state,[course()],2)
    _,event=step(state,[course()],3)
    assert event is None

def test_known_not_yet_open_can_be_released():
    state,_=step(empty_state(),[course(status='not_open')])
    state,event=step(state,[course()],15)
    assert event is None
    _,event=step(state,[course()],30)
    assert event is not None

@pytest.mark.parametrize('status',['unknown','closed'])
def test_ambiguous_or_closed_slot_cannot_trigger_after_opening(status):
    state,_=step(empty_state(),[])
    state,_=step(state,[course(status=status)],15)
    assert not state['weeks']['2026-11-16']['known']['1']['candidate']
    stored = state['weeks']['2026-11-16']['known']['1']
    stored['candidate']=True  # legacy persisted state
    stored.pop('candidate_checked')
    state,_=step(state,[course()],30)
    _,event=step(state,[course()],45)
    assert event is None

def test_release_after_berlin_midnight_belongs_to_new_target_week():
    sunday = NOW.replace(hour=23, minute=59)
    state,_=step(empty_state(),[course()],now=sunday)
    state,_=step(state,[],1,sunday)
    next_course=course(day='2026-11-23')
    state,event=step(state,[next_course],16,sunday)
    assert event is None
    _,event=step(state,[next_course],31,sunday)
    assert event['id']=='release:2026-11-23'

def test_rename_time_edit_removal_and_reappearance_are_not_new():
    state,_=step(empty_state(),[course()])
    state,_=step(state,[],10)
    changed=replace(course(),title='Neuer Titel',time='19:00')
    state,_=step(state,[changed],20)
    _,event=step(state,[changed],30)
    assert event is None

def test_failed_gap_requires_reconfirmation():
    state,_=step(empty_state(),[])
    state,_=step(state,[course()],1)
    state,event=step(state,[course()],120)
    assert event is None
    _,event=step(state,[course()],135)
    assert event is not None

def test_disappearance_resets_confirmation():
    state,_=step(empty_state(),[])
    state,_=step(state,[course()],1)
    state,_=step(state,[],2)
    _,event=step(state,[course()],3)
    assert event is None

def test_rapid_duplicate_run_does_not_confirm():
    state,_=step(empty_state(),[])
    state,_=step(state,[course()],1)
    _,event=step(state,[course()],1)
    assert event is None

def test_week_rollover_does_not_announce_preexisting_slots():
    state,_=step(empty_state(),[])
    state,_=step(state,[course(day='2026-11-23')],0,NOW+timedelta(days=1))
    _,event=step(state,[course(day='2026-11-23')],15,NOW+timedelta(days=1))
    assert event is None

def test_state_not_mutated_and_outbox_survives_restart(tmp_path):
    original=empty_state()
    state,_=step(original,[])
    state,_=step(state,[course()],15)
    state,event=step(state,[course()],30)
    assert original==empty_state()
    path=tmp_path/'state.json'
    atomic_write(path,state)
    loaded=load_state(path)
    assert loaded['events'][event['id']]==event
    _,again=step(loaded,[course()],45)
    assert again is None

def test_corrupt_state_is_not_reset(tmp_path):
    p=tmp_path/'state.json'; p.write_text('{"version":1}')
    with pytest.raises(VerificationError): load_state(p)

def fixture(name):
    return json.loads((FIXTURES/name).read_text(encoding='utf8'))

def test_actual_javascript_calendar_and_rendered_views():
    week=parse_week(fixture('week-2026-11-16.json'),MONDAY)
    free=parse_free(fixture('free-2026-11-16.json'),MONDAY)
    result=combine(week,free,NOW.date())
    assert len(result)==11
    assert all(c.status=='bookable' for c in result.values())
    assert (result['74654938'].time,result['74654938'].used,result['74654938'].capacity)==('09:00',4,10)
    assert result['74654939'].title=='Line Dance'

def test_actual_empty_week_is_distinguished_from_unreadable():
    assert parse_week(fixture('week-2026-11-23.json'),date(2026,11,23))=={}
    broken=fixture('week-2026-11-23.json'); broken['html']='<div id="container"></div>'
    with pytest.raises(VerificationError): parse_week(broken,date(2026,11,23))

def test_actual_empty_availability_is_confirmed_not_a_failure():
    empty=fixture('free-2026-11-23.json')
    assert parse_free(empty,date(2026,11,23))=={}
    empty['html']=empty['html'].replace('Kein verfügbarer Termin gefunden','Wird geladen')
    with pytest.raises(VerificationError): parse_free(empty,date(2026,11,23))

@pytest.mark.parametrize('mutation',['date','missing_chip','time','schedule','timezone'])
def test_parser_rejects_changed_or_partial_calendar(mutation):
    data=fixture('week-2026-11-16.json')
    if mutation=='date': data['cursor']='2026-11-09T00:00:00Z'
    if mutation=='missing_chip': data['html']=data['html'].replace('class="chip rd"','class="broken"',1)
    if mutation=='time': data['app'][0][0]+=3600
    if mutation=='schedule': data['rp_id']=123
    if mutation=='timezone': data['local']=True
    with pytest.raises(VerificationError): parse_week(data,MONDAY)

def test_unverified_pagination_rejected():
    data=fixture('free-2026-11-16.json'); data['total']=101
    with pytest.raises(VerificationError): parse_free(data,MONDAY)

def test_feed_contains_only_studio3_week_and_aggregate_release():
    state,_=step(empty_state(),[])
    state,_=step(state,[course()],15)
    state,event=step(state,[course()],30)
    feed=build_feed(state,{'1':course()},NOW+timedelta(minutes=30))
    assert feed['source']==BASE and feed['week_start']=='2026-11-16'
    assert feed['events']==[event] and feed['available_count']==1
    assert 'participants' not in json.dumps(feed)

def test_current_week_is_display_only_and_leaves_release_events_unchanged():
    current = course(day='2026-11-13')
    state = empty_state()
    feed = build_feed(state, {}, NOW, {current.slot_id: current})
    assert feed['week_start'] == '2026-11-16'
    assert feed['courses'] == [] and feed['events'] == []
    assert feed['current_week']['week_start'] == '2026-11-09'
    assert feed['current_week']['courses'][0]['day'] == '2026-11-13'
    assert state == empty_state()
