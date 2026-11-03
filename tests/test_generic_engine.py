"""Cross-industry engine integration, isolated from all live services."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

IMPORT_DATA = TemporaryDirectory(prefix='news-catch-generic-import-')
os.environ['TREND_DATA_DIR'] = IMPORT_DATA.name
os.environ['TREND_DISABLE_SCHEDULER'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'workbench'))
import engine as e
import classification as taxonomy
from rules import contains, import_rules


class GenericEngineTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix='news-catch-generic-engine-')
        self.addCleanup(temp.cleanup)
        location = Path(temp.name)
        for name, value in [('DATA', location), ('DB', location / 'trenddesk.sqlite3'),
                            ('CONFIG_FILE', location / 'config.json')]:
            fixture = patch.object(e, name, value)
            fixture.start(); self.addCleanup(fixture.stop)
        no_network = patch.object(e, 'request_bytes', side_effect=AssertionError('Live network forbidden in tests'))
        no_network.start(); self.addCleanup(no_network.stop)
        e.init_db()

    def profile(self, **changes):
        p = deepcopy(taxonomy.DEFAULT_PROFILE)
        p.update(industry='医疗健康', context='关注临床研究及医院运营',
                 categories=[dict(name='临床研究', description='药品和医疗服务的临床试验', keywords=['trial', 'pilot', '临床']),
                             dict(name='医院运营', description='机构运营和财务信息', keywords=['earnings', 'revenue'])],
                 actions=['研究试验结果', '同步团队'],
                 regions=[dict(name='英国', aliases=['UK', 'United Kingdom'])])
        p.update(changes)
        return p

    def article(self, **changes):
        a = dict(fingerprint='generic-test', title='Horizon Health begins UK pilot',
                 body='Horizon Health began a pilot in the UK for a new outpatient service.',
                 link='https://health.example.invalid/updates/1', source='Health Bulletin',
                 site_name='Health Bulletin', level='B 级', published_at=e.now(), matched_rules=['Health updates'])
        a.update(changes)
        return a

    def result(self, **changes):
        r = dict(title='Horizon Health 开始门诊服务试点，适用英国',
                 summary='Horizon Health 在英国开展新的门诊服务试点。',
                 evidence='Horizon Health began a pilot in the UK for a new outpatient service.',
                 evidence_zh='Horizon Health 在英国开始新门诊服务试点。',
                 region='英国', category='临床研究', stage='测试中', relevance='高',
                 action='研究试验结果', confidence='高', company='Horizon Health', language='',
                 change='在英国开展新的门诊服务试点。', site_name='Health Bulletin')
        r.update(changes)
        return r

    def add_article(self, **changes):
        a = self.article()
        r = e.fallback(a, 'fixture')
        r.update(title='已保存的人工标题', summary='已保存的人工摘要', **changes)
        return e.insert_event(a, r, e.datetime.now(e.TZ).date().isoformat())

    def test_defaults_are_neutral_and_custom_profile_persists(self):
        self.assertEqual(e.config()['analysis_profile']['industry'], '')
        self.assertNotIn('车载 OTA', e.public_config()['analysis_options']['categories'])
        self.assertNotIn('语音能力', e.public_config()['analysis_options']['categories'])
        p = self.profile()
        e.save_config(dict(analysis_profile=p))
        self.assertEqual(e.config()['analysis_profile'], taxonomy.validate_profile(p))
        public = e.public_config()
        self.assertEqual(public['analysis_options']['categories'], ['临床研究', '医院运营', '未分类'])
        self.assertEqual(public['analysis_options']['actions'], p['actions'])
        self.assertEqual(public['region_options'], ['英国', '其他', '未涉及'])

    def test_invalid_profile_does_not_partially_save_other_fields(self):
        baseline = e.config()
        bad_profiles = [self.profile(categories=[]), self.profile(actions=[]),
                        self.profile(categories=[dict(name='重复', keywords=[]), dict(name='重复', keywords=[])]),
                        self.profile(excluded_keywords='earnings'), self.profile(context='x' * 6001)]
        for bad in bad_profiles:
            with self.subTest(profile=bad):
                with self.assertRaises(ValueError):
                    e.save_config(dict(analysis_profile=bad, time='09:30', ai_model='must-not-save'))
                self.assertEqual(e.config(), baseline)

    def test_keyword_fallback_is_configured_and_ambiguous_is_unclassified(self):
        c = {**e.config(), 'analysis_profile': self.profile()}
        self.assertEqual(e.classify('New clinical trial begins', c), '临床研究')
        self.assertEqual(e.classify('医院公布临床研究', c), '临床研究')
        self.assertEqual(e.classify('Completely unrelated headline', c), '未分类')
        self.assertEqual(e.classify('trial earnings', c), '未分类')
        r = e.fallback(self.article(title='Completely unrelated headline', body='No matching terms.'), 'No AI', c)
        self.assertEqual(r['category'], '未分类')
        self.assertEqual(r['status'], '待审核')
        self.assertEqual(r['confidence'], '低')

    def test_exclusions_are_opt_in_for_finance_retail_and_hiring(self):
        c = e.config()
        for title in ('Retail earnings rise', 'Company hiring staff', 'Store discount offer', 'Hospital awards contract'):
            self.assertFalse(e.noise(title, c))
        c['analysis_profile'] = self.profile(excluded_keywords=['coupon', '招聘广告'])
        self.assertTrue(e.noise('New coupon available', c))
        self.assertTrue(e.noise('某机构招聘广告发布', c))
        self.assertFalse(e.noise('Retail earnings rise', c))

    def test_configured_model_receives_industry_taxonomy_and_returns_custom_values(self):
        e.save_config(dict(analysis_profile=self.profile(), ai_url='https://model.example.invalid/v1',
                           ai_key='test-only-key', ai_model='test-model'))
        response = json.dumps(dict(choices=[dict(message=dict(content=json.dumps(self.result(), ensure_ascii=False)))]))
        with patch.object(e, 'model_request', return_value=response) as model:
            result = e.analyze(self.article(manual_source=True), e.config(), strict=True)
        url, request, c = model.call_args.args
        self.assertEqual(url, 'https://model.example.invalid/v1/chat/completions')
        self.assertEqual(request['model'], 'test-model')
        self.assertEqual(c['ai_key'], 'test-only-key')
        system = request['messages'][0]['content']
        self.assertIn('医疗健康', system)
        self.assertIn('临床研究', system)
        self.assertIn('关注临床研究及医院运营', system)
        self.assertNotIn('海外车载语音本土化', system)
        self.assertEqual(result['category'], '临床研究')
        self.assertEqual(result['action'], '研究试验结果')
        self.assertEqual(result['company'], 'Horizon Health')

    def test_unconfigured_industry_model_analyzes_financial_news(self):
        c = e.config(); c.update(ai_url='https://model.example.invalid', ai_key='fixture', ai_model='fixture')
        article = self.article(title='Retail cooperative reports earnings',
                               body='The retail cooperative reported annual earnings of 12 million euros.', manual_source=True)
        output = self.result(title='零售合作社公布年度盈利1200万欧元', summary='零售合作社公布年度盈利1200万欧元。',
                             evidence=article['body'], region='未涉及', category='市场与经营', stage='',
                             action='持续观察', company='零售合作社', change='年度盈利为1200万欧元。')
        response = json.dumps(dict(choices=[dict(message=dict(content=json.dumps(output, ensure_ascii=False)))]))
        with patch.object(e, 'model_request', return_value=response) as model:
            result = e.analyze(article, c, strict=True)
        self.assertEqual(result['category'], '市场与经营')
        self.assertIn('不默认任何行业', model.call_args.args[1]['messages'][0]['content'])

    def test_model_rejects_values_outside_current_taxonomy(self):
        c = {**e.config(), 'analysis_profile': self.profile()}
        schema = {key: '' for key in self.result()}
        for key, invalid in [('category', '车载 OTA'), ('action', '体验验证')]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    e.validate_analysis(self.result(**{key: invalid}), self.article(), schema, c)

    def test_webhook_preserves_any_subject_and_languages_and_scalar_region(self):
        e.save_config(dict(analysis_profile=self.profile()))
        eid = self.add_article(company='Horizon Health / 城市门诊合作社', category='临床研究',
                               region='United Kingdom', language='葡萄牙语、阿拉伯语', status='待推送')
        values = e.webhook_values(e.event_detail(eid), set())
        self.assertEqual(values['company'], 'Horizon Health / 城市门诊合作社')
        self.assertEqual(values['category'], '临床研究')
        self.assertEqual(values['region'], '英国')
        self.assertIsInstance(values['region'], str)
        self.assertEqual(values['language'], ['葡萄牙语', '阿拉伯语'])

    def test_named_feed_rule_does_not_add_hidden_capability_gate(self):
        source = dict(name='Retail bulletin', url='https://retail.example.invalid/feed', kind='rss',
                      enabled=True, level='B 级')
        e.save_config(dict(sources=[source], rules=[dict(name='Feed 2 arbitrary name', expression='earnings', scope='title')]))
        article = self.article(title='Retail cooperative earnings grow',
                               body='The cooperative reported higher annual earnings.')
        with patch.object(e, 'rss', return_value=[article]):
            result = e.run_pipeline()
        self.assertEqual(result['candidate_count'], 1)
        self.assertEqual(e.get_events()[0]['original_title'], article['title'])

    def test_taxonomy_change_preserves_history_and_old_label_can_be_saved(self):
        eid = self.add_article(category='旧行业类别', action='旧行业动作')
        original = e.event_detail(eid)
        e.save_config(dict(analysis_profile=self.profile()))
        self.assertEqual(e.event_detail(eid), original)
        saved = e.update_event(dict(id=eid, revision=original['revision'], category='旧行业类别',
                                    action='旧行业动作', summary='人工修订摘要'))['article']
        self.assertEqual(saved['category'], '旧行业类别')
        newer = e.update_event(dict(id=eid, revision=saved['revision'], category='临床研究',
                                    action='研究试验结果'))['article']
        self.assertEqual(newer['category'], '临床研究')
        with self.assertRaises(ValueError):
            e.update_event(dict(id=eid, revision=newer['revision'], category='未知新类别'))

    def test_changed_profile_during_analysis_cannot_write_stale_result(self):
        e.save_config(dict(analysis_profile=self.profile()))
        eid = self.add_article(); row = e.event_detail(eid); c = e.config()
        def change_config(*args, **kwargs):
            e.save_config(dict(analysis_profile=self.profile(industry='能源')))
            return {**self.result(), 'analysis_mode': 'fixture', 'status': '待审核'}
        with patch.object(e, 'analyze', side_effect=change_config):
            try:
                result = e.analyze_saved(row, c, manual=True)
            except ValueError:
                result = {'ok': False}
        self.assertFalse(result['ok'])
        self.assertEqual(e.event_detail(eid)['title'], row['title'])
        self.assertEqual(e.event_detail(eid)['revision'], row['revision'])

    def test_deleted_during_analysis_cannot_be_restored_by_result(self):
        eid = self.add_article(); row = e.event_detail(eid)
        def delete(*args, **kwargs):
            e.deletion(dict(id=eid, revision=row['revision']))
            return {**self.result(), 'analysis_mode': 'fixture', 'status': '待审核'}
        with patch.object(e, 'analyze', side_effect=delete):
            try:
                result = e.analyze_saved(row, e.config(), manual=True)
            except ValueError:
                result = {'ok': False}
        self.assertFalse(result['ok'])
        self.assertEqual(e.event_detail(eid)['status'], '已删除')
        self.assertEqual(e.event_detail(eid)['title'], row['title'])

    def test_eighty_custom_regions_roundtrip_with_reserved_options(self):
        regions = [dict(name=f'自定义地区{i}', aliases=[f'Custom region {i}']) for i in range(80)]
        e.save_config(dict(analysis_profile=self.profile(regions=regions)))
        saved = e.config()
        self.assertEqual(len(saved['analysis_profile']['regions']), 82)
        self.assertEqual(e.public_config()['region_options'][-2:], ['其他', '未涉及'])
        e.save_config(dict(analysis_profile=saved['analysis_profile']))
        self.assertEqual(e.config(), saved)
        e.save_config(dict(time='09:30'))
        baseline = e.config()
        with self.assertRaises(ValueError):
            e.save_config(dict(analysis_profile=self.profile(regions=regions + [dict(name='第81个地区', aliases=[])]),
                               time='08:00'))
        self.assertEqual(e.config(), baseline)

    def test_recommendation_section_does_not_hide_subsequent_article_facts(self):
        article = self.article(body=('## Clinical results\nThe study enrolled 40 people.\n'
                                     '## Related articles\nOther stories you might read.\n'
                                     '## Funding details\nThe study received a grant of $2 million.'))
        scoped, sections = e.prepare_article(article)
        facts = [dict(section_id=s['id'], name='推荐内容' if s['heading']=='Related articles' else '研究进展',
                      change='推荐其他文章。' if s['heading']=='Related articles' else '研究披露具体进展。',
                      evidence=s['text']) for s in sections]
        inventory, _ = e.brief_inventory(scoped, sections, facts)
        self.assertEqual([fact['auxiliary'] for fact in inventory], [False, True, False])
        self.assertEqual(inventory[-1]['heading'], 'Funding details')

    def test_short_section_analysis_preserves_validated_model_headline(self):
        opening = 'The cooperative opened 8 stores in Spain.'
        pricing = 'The cooperative reduced coffee prices from $4.80 to $4.50 in Spain.'
        article = self.article(title='Cooperative expands stores and reduces prices in Spain',
                               body=f'## Store openings\n{opening}\n## Pricing\n{pricing}')
        headline = '合作社在西班牙新开8家门店并将咖啡价格降至$4.50'
        output = self.result(title=headline,
                             summary='合作社在西班牙扩大门店经营并调整咖啡价格，新增8家门店，咖啡价格由$4.80降至$4.50。',
                             evidence=opening, category='市场与经营', stage='已上线', region='欧洲',
                             action='持续观察', company='Cooperative', change='门店和价格发生变化。')
        output['features'] = [
            dict(section_id='s1', name='门店扩张', change='合作社在西班牙新开8家门店。', evidence=opening,
                 stage='已上线', stage_evidence=opening, scope='西班牙', scope_evidence='Spain'),
            dict(section_id='s2', name='咖啡价格', change='合作社在西班牙将咖啡价格从$4.80降至$4.50。', evidence=pricing,
                 stage='已上线', stage_evidence=pricing, scope='西班牙', scope_evidence='Spain'),
        ]
        output['summary_fact_ids'] = ['f1', 'f2']
        output['key_changes'] = [dict(text='在西班牙新开8家门店。', fact_ids=['f1']),
                                 dict(text='西班牙咖啡价格由$4.80调整至$4.50。', fact_ids=['f2'])]
        c = e.config(); c.update(ai_url='https://model.example.invalid', ai_key='fixture', ai_model='fixture')
        response = json.dumps(dict(choices=[dict(message=dict(content=json.dumps(output, ensure_ascii=False)))]))
        with patch.object(e, 'model_request', return_value=response) as model:
            result = e.analyze(article, c, strict=True)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['title'], headline)
        self.assertIn('$4.50', result['title'])
        self.assertIn('西班牙', result['title'])

    def test_boolean_rule_contains_chinese_substrings_without_breaking_english_boundaries(self):
        self.assertTrue(contains('这项临床研究纳入新患者', '临床研究'))
        self.assertTrue(contains('零售商公布年度营收增长', '营收'))
        self.assertFalse(contains('A hospital pilot', 'IPO'))
        self.assertTrue(contains('An IPO was announced', 'IPO'))
        rule = dict(name='行业筛选', expression='临床 AND NOT 招聘', scope='title_content')
        self.assertEqual(e.match_rules('某医院发布研究', '新的临床成果公开', {**e.config(), 'rules':[rule]}), ['行业筛选'])
        self.assertEqual(e.match_rules('医院招聘公告', '临床工作人员', {**e.config(), 'rules':[rule]}), [])

    def test_import_rules_accepts_any_named_single_or_five_groups(self):
        for count in (1, 5):
            with self.subTest(count=count), TemporaryDirectory(prefix='news-catch-rules-') as folder:
                path = Path(folder) / 'rules.md'
                path.write_text('\n\n'.join(f'### 自定义行业规则{i}\n\n```text\n("clinical trial" OR 临床) NOT 招聘\n```'
                                            for i in range(count)), encoding='utf-8')
                rules = import_rules(path)
                self.assertEqual(len(rules), count)
                self.assertEqual([rule['name'] for rule in rules], [f'自定义行业规则{i}' for i in range(count)])
                self.assertTrue(all(rule['scope']=='title_content' for rule in rules))
                self.assertEqual(e.match_rules('医院研究进展', '开展临床试验', {**e.config(), 'rules':rules}),
                                 [rule['name'] for rule in rules])


if __name__ == '__main__':
    unittest.main()
