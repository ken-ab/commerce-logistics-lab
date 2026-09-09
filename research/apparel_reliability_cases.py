"""New event combinations for reliability validation; no model or agent imports."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

from apparel_fulfillment.data import digest
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import departures, instant, iso, load_corridor
from research.apparel_candidate_validation import clone, read, save
from research.apparel_expansion_cases import OWNER

ROOT=Path(__file__).resolve().parents[1]
FAMILIES=('short_delay','long_delay','alternate_delayed','cancellation','future_event','unrelated_event',
          'downstream_wait','exhausted_departures','second_revision','approved_variant','mixed_pack_revision','read_only_control')


def cases():
    base=read(ROOT/'data/apparel_fulfillment_expansion_v1.json')
    added=sorted(set(base['variants'])-set(read(ROOT/'data/apparel_fulfillment_v1.json')['variants']))
    rows=[]
    def pick(n,predicate):
        pool=[s for s in added if predicate(base['variants'][s])]
        return pool[n%len(pool)]
    def line(ident,sku,variation,*,packs=False):
        v=base['variants'][sku];units=math.ceil(12/v['pieces_per_catalog_unit'])+variation+2
        return {'line_id':ident,'requested_sku':sku,'quantity':units if packs else units*v['pieces_per_catalog_unit'],
            'unit':'catalog_unit' if packs else 'piece',
            **{k:v[k] for k in ('style_id','brand','category','size','color')},
            **({'audience':v['audience']} if v.get('audience') else {})}
    for index,family in enumerate(FAMILIES):
        for variation in range(2):
            n=index*29+variation*61+17
            first=pick(n,lambda v:v['pieces_per_catalog_unit']==1 and v['category']=='t_shirt')
            if family=='mixed_pack_revision':first=pick(n,lambda v:v['pieces_per_catalog_unit']>1)
            second=pick(n+23,lambda v:v['category']=='hoodie' and v['brand']!=base['variants'][first]['brand'])
            now=datetime(2028,4,5,2,tzinfo=timezone.utc)+timedelta(days=4*index+variation)
            world=deepcopy(base)
            for stock in world['stock'].values():stock['available_catalog_units']=160
            req={'sales_region':'DE','wholesale':True,'needs_shipping':True,
                'shipping':{'destination':'DE-DC','budget_cents':45000,'ready_at':iso(now+timedelta(days=1)),
                            'deadline_at':iso(now+timedelta(days=9))},
                'lines':[line('tops',first,variation,packs=family=='mixed_pack_revision')]}
            selections=[{'line_id':'tops','sku':first}]
            if family in ('downstream_wait','mixed_pack_revision'):
                req['lines'].append(line('outerwear',second,variation));selections.append({'line_id':'outerwear','sku':second})
            allowed={s['line_id']:[s['sku']] for s in selections}
            approve=None
            if family=='approved_variant':
                approve=pick(n+11,lambda v:v['sku']!=first and v['brand']==world['variants'][first]['brand']
                    and v['category']=='t_shirt' and v['pieces_per_catalog_unit']==1)
                allowed['tops']=[approve]
            expected={'order_status':'ready','decision_status':'ready','proposal':'revise','read_only':False,
                      'required_tools':['read_proposal','read_transport_events'],'read_variant':True,
                      'allowed':allowed,'issue':None,'old_valid':False}
            contract={'mode':'review_proposal'}
            task='请复核当前订单的运输提案：核对实际选中服装的资料与已发布运输变化。旧方案还能用就保留；不能用则按原预算和交期修订，说明原班次与最终选用班次的区别，列出费用、到达时间及依据，等我单独确认。'
            if family in ('future_event','unrelated_event'):
                expected.update(proposal='keep',old_valid=True)
            if family=='exhausted_departures':expected.update(proposal='infeasible',decision_status='unfulfillable')
            if family=='approved_variant':task+=' 已批准的替代选择保留，不要换回最初要求的SKU或重复索取批准。'
            if family=='mixed_pack_revision':task+=' 两行采购的单位不同，请保留行与商品的对应关系。'
            if family=='read_only_control':
                contract={'mode':'check_order'}
                task='只核验当前服装订单是否满足数量、库存和品牌规则，指出真实存在的问题。不要生成或修订运输提案，不要改动采购要求。'
                expected.update(proposal='none',read_only=True,required_tools=[],read_variant=False,old_valid=None)
                if variation:
                    world['stock'][first]['available_catalog_units']=5
                    expected.update(order_status='unfulfillable',decision_status='unfulfillable',issue='insufficient_stock')
            rows.append({'id':f'RL-{index:02d}-{variation}','family':family,'variation':variation,'now':iso(now),
                'world':world,'request':req,'initial_selections':selections,'approve_sku':approve,
                'contract':contract,'task':task,'expected':expected})
    return rows


def setup(case,folder):
    folder.mkdir(parents=True)
    save(folder/'world.json',case['world'])
    store=ApparelStore(folder/'operations.sqlite',world=case['world'])
    draft=store.create_draft(OWNER,case['request'])
    draft=store.select(OWNER,draft['id'],case['initial_selections'],expected_revision=draft['revision'])
    if case['approve_sku']:
        draft=store.select(OWNER,draft['id'],[{'line_id':'tops','sku':case['approve_sku']}],expected_revision=draft['revision'])
        draft=store.approve_substitution(OWNER,draft['id'],draft['order_check']['substitution_proposals'][0]['approval_id'],expected_revision=draft['revision'])
    old=None;now=instant(case['now']);family=case['family']
    if family!='read_only_control':
        old=store.propose(OWNER,draft['id'],expected_revision=draft['revision'],now=now)
        assert old['route']['status']=='planned'
        def air(p):return next(s for s in p['route']['segments'] if s['mode']=='air')
        def add(ident,seg,kind='delay',delay=0,published=None):
            store.add_transport_event({'event_id':case['id']+'-'+ident,'leg_id':seg['leg_id'],'nominal_departure':seg['nominal_departure'],
                'kind':kind,'delay_minutes':delay,'published_at':published or case['now']})
        original=air(old)
        if family=='short_delay':add('short',original,delay=45+case['variation']*45)
        elif family in ('long_delay','alternate_delayed','approved_variant','mixed_pack_revision'):
            add('long',original,delay=1620+case['variation']*180)
            if family=='alternate_delayed':
                alternative={**original,'nominal_departure':iso(instant(original['nominal_departure'])+timedelta(hours=12))}
                add('next',alternative,delay=75+case['variation']*30)
        elif family=='cancellation':
            add('late',original,delay=90);add('cancel',original,kind='cancel')
        elif family=='downstream_wait':add('cancel',original,kind='cancel')
        elif family=='future_event':add('future',original,kind='cancel',published=iso(now+timedelta(days=2)))
        elif family=='unrelated_event':
            sea=next(leg for leg in store.corridor['legs'] if leg['mode']=='sea')
            nominal=next(departures(sea,now,now+timedelta(days=15),store.corridor,[]))[0]
            add('sea',{'leg_id':sea['id'],'nominal_departure':iso(nominal)},kind='cancel')
        elif family=='second_revision':
            add('first',original,kind='cancel')
            old=store.propose(OWNER,draft['id'],expected_revision=draft['revision'],now=now)
            assert old['version']==2
            add('second',air(old),delay=1800)
        elif family=='exhausted_departures':
            # Cancel every air service before the fixed deadline. Sea/rail exceed it.
            leg=next(x for x in store.corridor['legs'] if x['mode']=='air')
            for i,(nominal,_,_) in enumerate(departures(leg,instant(case['request']['shipping']['ready_at']),
                    instant(case['request']['shipping']['deadline_at']),store.corridor,[])):
                add(str(i),{'leg_id':leg['id'],'nominal_departure':iso(nominal)},kind='cancel')
    view=store.view(OWNER,draft['id'])
    seed={'draft_id':draft['id'],'old_id':old['proposal_id'] if old else None,'view_digest':digest(view),
          'events_digest':digest(store.transport_events())}
    save(folder/'seed.json',seed)
    return store,seed


def fixture_check(case,initial,seed,path):
    store=clone(initial,path);before=store.view(OWNER,seed['draft_id']);expected=case['expected']
    assert before['request']==case['request'] and before['order_check']['status']==expected['order_status']
    assert {s['line_id']:s['sku'] for s in before['selections']}=={k:v[0] for k,v in expected['allowed'].items()}
    old=before['proposals'][-1] if before['proposals'] else None
    if seed['old_id']:
        valid=store.assess(OWNER,seed['draft_id'],seed['old_id'],now=instant(case['now']))
        assert valid['valid']==expected['old_valid'],(case['id'],valid)
    result=None
    if expected['proposal'] in ('revise','infeasible'):
        result=store.propose(OWNER,seed['draft_id'],expected_revision=before['revision'],now=instant(case['now']))
        assert result['previous_proposal_id']==seed['old_id'] and result['version']==old['version']+1
        assert result['route']['status']==('infeasible' if expected['proposal']=='infeasible' else 'planned')
        if expected['proposal']=='revise':
            assert store.assess(OWNER,seed['draft_id'],result['proposal_id'],now=instant(case['now']))['valid']
            a=next(s for s in old['route']['segments'] if s['mode']=='air')
            b=next(s for s in result['route']['segments'] if s['mode']=='air')
            assert (a['service_id']==b['service_id'])==(case['family']=='short_delay')
    after=store.view(OWNER,seed['draft_id'])
    assert after['request']==before['request'] and after['approved_substitutions']==before['approved_substitutions'] and after['confirmation'] is None
    return {'case_id':case['id'],'passed':True,'proposal_action':expected['proposal'],
        'initial_versions':len(before['proposals']),'result_route_status':result['route']['status'] if result else None,
        'events':len(store.transport_events()),'new_model_calls':0}
