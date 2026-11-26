import pandas as pd
from model.crowd import fit_profile, crowd_label, seat_chance

def test_relative_profile_and_label():
    frame=pd.DataFrame([
        {'route_id':'A','segment_id':'s1','hour':8,'day_type':'weekday','load_proxy':x}
        for x in range(1,21)
    ])
    profile=fit_profile(frame,min_group=2)
    assert 0 <= profile.iloc[0].crowd_score <= 100
    assert crowd_label(10)['hi']=='Kam bheed'
    assert crowd_label(90)['hi']=='Bahut bheed'

def test_early_boarding_improves_seat_chance():
    assert seat_chance(45,0)=='Good chance of a seat'
