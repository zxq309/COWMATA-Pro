def test_candidate_roles_preserve_unknown_and_conflicts():
    from cowmata_tailring.workspace.algorithm_dataset import candidate_role
    event = dict(event_id='e',code='STANDING_UP',start_ms=10000,end_ms=14000,
                 role='positive',negative_for=[],training_eligible=True)
    assert candidate_role(12,[event],'STANDING_UP') == ('P','e')
    assert candidate_role(20,[event],'STANDING_UP') == ('U','')
    assert candidate_role(12,[{**event,'training_eligible':False}],'STANDING_UP') == ('IGNORE','')
    negative = {**event,'event_id':'n','role':'negative','negative_for':['STANDING_UP']}
    assert candidate_role(12,[negative],'STANDING_UP') == ('N','n')
    assert candidate_role(12,[event,negative],'STANDING_UP') == ('IGNORE','')


def test_consumer_tables_separate_cows_and_never_train_unknown(tmp_path):
    import pandas as pd

    from cowmata_tailring.workspace.algorithm_dataset import save_consumer
    x = pd.DataFrame({'feature':[1.,2.,3.,4.]})
    m = pd.DataFrame({'candidate_id':['a','b','c','d'],'session_key':['s']*4,
        'cow_group':['21100','21100','23335','21100'],'spot_s':[1.,2.,3.,4.],
        'role':['P','U','N','N'],'event_id':['e','','n','n2'],
        'split':['train','train','test','train']})
    result = save_consumer(tmp_path,'起立',x,m,[])
    rows = pd.read_csv(tmp_path/'training_candidates.csv')
    assert set(rows.candidate_id) == {'a','d'}
    assert set(rows.target) == {0,1}
    assert result['unknown_candidates'] == 1
    assert result['ready_to_fit']
