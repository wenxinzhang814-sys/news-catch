"""Cross-industry evidence regressions; no live model, database or Webhook."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workbench'))
from analysis_quality import (apply_features, claims_released, feature_title,
                              prepare_article, validate_features, validate_polarity)
from analysis_summary import auxiliary, inventory, validate_brief
from long_analysis import extract, sections_for, split_text, validate_inventory
from page_changes import change_set, validate_change_facts


def fact(text, *, stage='待确认', change='原文披露该事项的具体变化。', scope='', scope_evidence=''):
    return dict(section_id='s1',name='事项进展',change=change,evidence=text,
                stage=stage,stage_evidence=text if stage!='待确认' else '',
                scope=scope,scope_evidence=scope_evidence)


class GenericSourceTests(unittest.TestCase):
    def article(self,body,link='https://example.invalid/news/1'):
        return dict(title='原始行业新闻',body=body,link=link)

    def test_url_and_vendor_never_discard_body_or_rewrite_title(self):
        body=('Introduction and background.\n\n## Current announcement\n'
              'Tesla Payment feature in update 4.62.0\nCurrent facts.\n\n'
              '## Historical comparison\nTesla Previous feature in update 4.61.0\nOlder facts.\n')
        for link in ('https://www.notateslaapp.com/tesla-app-updates/version/4.62.0/release-notes',
                     'https://example.invalid/version/9.0/research'):
            with self.subTest(link=link):
                original=self.article(body,link);scoped,sections=prepare_article(original)
                self.assertEqual(scoped,original)
                self.assertEqual(''.join(s['text'] for s in sections),body)
                self.assertEqual(len(sections),3)

    def test_markdown_html_and_introduction_are_lossless(self):
        body='  \nOverview.\n\n# Clinical findings\nTrial results.\n<h2>Policy &amp; <em>funding</em></h2>\nFunding changes.\n'
        scoped,sections=prepare_article(self.article(body))
        self.assertEqual(scoped['body'],body)
        self.assertEqual([s['heading'] for s in sections],['文章概述','Clinical findings','Policy & funding'])
        self.assertEqual(''.join(s['text'] for s in sections),body)

    def test_single_heading_and_end_heading_are_covered(self):
        for body in ('## Retail\nStores now open.','Intro\n## Energy'):
            with self.subTest(body=body):
                _,sections=prepare_article(self.article(body))
                self.assertEqual(''.join(s['text'] for s in sections),body)

    def test_code_fences_are_not_news_sections(self):
        body='# Actual facts\nText\n```markdown\n## Example heading\n```\n## More facts\nMore text.'
        _,sections=prepare_article(self.article(body))
        self.assertEqual([s['heading'] for s in sections],['Actual facts','More facts'])
        self.assertEqual(''.join(s['text'] for s in sections),body)

    def test_no_heading_keeps_whole_body(self):
        original=self.article('An ordinary energy-sector announcement without headings.')
        scoped,sections=prepare_article(original)
        self.assertEqual(scoped,original)
        self.assertEqual(sections,[])

    def test_long_partition_and_short_heading_merge_are_exact(self):
        body='Intro\n# A\n'+('Clinical research data, findings and limitations.\n'*220)+'# B\nEnd.'
        scoped,sections=prepare_article(self.article(body))
        self.assertEqual(''.join(p['text'] for p in split_text(body)),body)
        self.assertEqual(''.join(p['text'] for p in sections_for(scoped,sections)),body)


class GenericEvidenceTests(unittest.TestCase):
    def validate(self,text,**kwargs):
        return validate_features([fact(text,**kwargs)],[dict(id='s1',heading='事项',text=text)])[0]

    def test_clinical_trials_and_retail_pilots_are_not_implemented(self):
        cases=[('The hospital began a phase 3 clinical trial in France.','医院在法国开展临床试验。'),
               ('The retailer is running a pilot programme in 12 stores.','零售商在12家门店开展试点。')]
        for text,change in cases:
            with self.subTest(text=text):
                self.assertEqual(self.validate(text,stage='测试中',change=change)['stage'],'测试中')
                with self.assertRaises(ValueError):self.validate(text,stage='已上线',change=change)

    def test_energy_investment_and_policy_future_effect_are_announced(self):
        cases=[('The utility plans to invest $50 million in solar capacity.','能源企业计划投资50百万美元建设太阳能项目。'),
               ('The regulation will take effect on 2028-01-01.','该规定将于2028-01-01生效。'),
               ('The agency published a proposed rule on packaging.','机构公布包装规定草案，提议调整要求。')]
        for text,change in cases:
            with self.subTest(text=text):
                self.assertEqual(self.validate(text,stage='仅宣布',change=change)['stage'],'仅宣布')
                with self.assertRaises(ValueError):self.validate(text,stage='已上线',change=change)
                with self.assertRaises(ValueError):self.validate(text,stage='仅宣布',change='该项目已实施。')

    def test_retail_and_effective_policy_are_not_software_only(self):
        for text,change in [('The retailer opened 8 stores in Spain.','零售商在西班牙新开8家门店。'),
                            ('The regulation is now effective in Germany.','该规定现已在德国生效。')]:
            with self.subTest(text=text):
                self.assertEqual(self.validate(text,stage='已上线',change=change)['stage'],'已上线')

    def test_negated_chinese_implementation_is_not_a_launch_claim(self):
        for text in ('尚未正式实施','未确认已生效','这不代表已投产','没有正式开业'):
            with self.subTest(text=text):self.assertFalse(claims_released(text))
        for text in ('规定已生效','项目正式投产','门店已开业'):
            with self.subTest(text=text):self.assertTrue(claims_released(text))

    def test_software_beta_remains_testing_and_explicit_removal_is_allowed(self):
        text='The beta release is now available for testers.'
        self.assertEqual(self.validate(text,stage='测试中',change='软件测试版向测试人员提供。')['stage'],'测试中')
        with self.assertRaises(ValueError):self.validate(text,stage='已上线',change='软件移除了Beta标签。')
        validate_polarity(dict(summary='软件移除Beta标签。',stage='已上线'),
                          dict(body='The release removed the beta label.'))

    def test_quotes_cannot_be_borrowed_across_industries_or_sections(self):
        sections=[dict(id='s1',heading='Healthcare',text='The hospital opened a new clinic.'),
                  dict(id='s2',heading='Retail',text='The retailer reduced prices.')]
        first=fact(sections[1]['text']);second={**fact(sections[1]['text']),'section_id':'s2'}
        with self.assertRaises(ValueError):validate_features([first,second],sections)
        with self.assertRaises(ValueError):validate_features([fact(sections[0]['text'])],sections)

    def test_global_scope_requires_own_explicit_evidence(self):
        text='The retailer opened stores in Spain.'
        with self.assertRaises(ValueError):self.validate(text,scope='全球',scope_evidence='Spain')
        text='The policy applies worldwide.'
        self.assertEqual(self.validate(text,scope='全球',scope_evidence='worldwide')['scope'],'全球')

    def test_mixed_stages_cannot_be_aggregated_into_implemented(self):
        result=dict(region='全球')
        facts=[fact('The clinic is now open.',stage='已上线'),
               fact('A clinical trial is running.',stage='测试中')]
        self.assertEqual(apply_features(result,facts)['stage'],'待确认')

    def test_verified_clinical_trial_does_not_block_independent_policy_publication(self):
        trial='The hospital began clinical trials.'
        policy='The agency formally published a regulation that is now effective.'
        sections=[dict(id='s1',heading='Hospital',text=trial),dict(id='s2',heading='Policy',text=policy)]
        facts=validate_features([
            {**fact(trial,stage='测试中',change='医院开展临床试验。'),'name':'临床试验'},
            {**fact(policy,stage='已上线',change='监管机构正式发布已生效的新规定。'),
             'section_id':'s2','name':'新规定'}],sections)
        output=apply_features(dict(region='未涉及'),facts)
        self.assertEqual(output['stage'],'待确认')
        validate_polarity(output,dict(body=trial+'\n'+policy),verified_features=facts)
        with self.assertRaises(ValueError):
            validate_polarity({**output,'stage':'已上线'},dict(body=trial+'\n'+policy),verified_features=facts)
        brief=validate_brief(dict(summary='医院开展临床试验，监管机构同时正式发布已生效的新规定。',
            summary_fact_ids=['f1','f2'],key_changes=[dict(text='医院开展临床试验。',fact_ids=['f1']),
            dict(text='监管机构正式发布已生效的新规定。',fact_ids=['f2'])]),inventory(facts))
        self.assertIn('正式发布',brief['summary'])

    def test_verified_inventory_does_not_waive_beta_or_stage_evidence(self):
        beta='The beta release is available for testers.'
        facts=[fact(beta,stage='测试中',change='软件测试版向测试人员提供。')]
        for output in (dict(summary='软件正式上线。',stage='已上线'),
                       dict(summary='软件正式发布。',stage='待确认')):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):validate_polarity(output,dict(body=beta),verified_features=facts)
        implemented=fact('The new regulation is effective.',stage='已上线',change='新规定已生效。')
        with self.assertRaises(ValueError):
            validate_polarity(dict(summary='软件移除了Beta标签，新规定已生效。',stage='待确认'),
                              dict(body=beta+' '+implemented['evidence']),verified_features=facts+[implemented])
        with self.assertRaises(ValueError):
            validate_polarity(dict(summary='软件正式上线。',stage='已上线'),dict(body=beta),
                              verified_features=[{**facts[0],'stage':'已上线'}])

    def test_generic_headlines_have_institution_subject_and_stage_boundaries(self):
        facts=[fact('A proposed rule.',stage='仅宣布',change='机构提议调整包装规则。')]
        title=feature_title(dict(company='环境监管机构'),facts,{})
        self.assertIn('环境监管机构',title)
        self.assertIn('宣布',title)
        self.assertNotIn('已实施',title)
        self.assertNotIn('产品',feature_title({},facts,{}))

    def test_policy_and_research_headings_are_valid_summary_evidence(self):
        for heading in ('Policy and legislation','Reports and studies','Factsheet / Infographic'):
            item={**fact('The study examined 40 participants.',change='研究纳入40名参与者。'),'heading':heading}
            with self.subTest(heading=heading):
                self.assertFalse(auxiliary(item))
                facts=inventory([item])
                output=validate_brief(dict(summary='研究披露了参与者范围。',summary_fact_ids=['f1'],
                                           key_changes=[dict(text='研究纳入40名参与者。',fact_ids=['f1'])]),facts)
                self.assertIn('40',output['change'])
        self.assertTrue(auxiliary({**fact('Related content'),'heading':'Related articles'}))

    def test_summary_cannot_invent_retail_numbers_or_policy_implementation(self):
        facts=inventory([fact('The retailer plans to open 8 stores.',stage='仅宣布',change='零售商计划开设8家门店。')])
        for summary in ('零售商计划开设9家门店。','零售商已开业8家门店。'):
            with self.subTest(summary=summary):
                with self.assertRaises(ValueError):
                    validate_brief(dict(summary=summary,summary_fact_ids=['f1'],
                                        key_changes=[dict(text=summary,fact_ids=['f1'])]),facts)

    def test_page_change_preserves_retail_price_old_and_new(self):
        details=change_set('Coffee | $4.50\nMilk | $2.00','Coffee | $4.80\nMilk | $2.00')
        parsed=dict(title='零售商将咖啡价格由$4.50调整为$4.80',change_facts=[dict(
            change_id='c1',kind='modified',name='咖啡价格',change='咖啡价格由$4.50调整为$4.80。',
            before_evidence='Coffee | $4.50',after_evidence='Coffee | $4.80')])
        self.assertEqual(len(validate_change_facts(parsed,details)),1)
        parsed['change_facts'][0]['change']='咖啡价格调整为$4.80。'
        with self.assertRaises(ValueError):validate_change_facts(parsed,details)

    def test_long_inventory_requires_all_energy_numeric_evidence(self):
        text='The solar project capacity is 200 MW and the cost is $50 million.'
        section=dict(id='s1',heading='Energy investment',text=text)
        self.assertEqual(len(validate_inventory(dict(features=[fact(text)]),[section])),1)
        incomplete=fact('The solar project capacity is 200 MW',change='太阳能项目产能为200兆瓦。')
        with self.assertRaises(ValueError):validate_inventory(dict(features=[incomplete]),[section])

    def test_parallel_extraction_uses_generic_transport_and_validated_cache(self):
        body='# Healthcare\nThe study enrolled 40 people.\n# Retail\nThe shop opened 8 stores.'
        scoped,sections=prepare_article(dict(title='Sector updates',link='https://example.invalid',body=body))
        calls=[]
        def request(messages):
            calls.append(messages)
            payload=json.loads(messages[1]['content'])
            return json.dumps(dict(features=[{**fact(s['text']),'section_id':s['id']} for s in payload['source_sections']]))
        with TemporaryDirectory(prefix='news-catch-generic-facts-') as cache:
            first,stats=extract(scoped,sections,None,request,json.loads,cache,'test-provider')
            second,cached=extract(scoped,sections,None,request,json.loads,cache,'test-provider')
        self.assertEqual(first,second)
        self.assertEqual(len(calls),1)
        self.assertEqual(stats['cached'],0)
        self.assertEqual(cached['cached'],1)
        self.assertIn('任意行业',calls[0][0]['content'])
        self.assertEqual(len(first),2)


if __name__=='__main__':unittest.main()
