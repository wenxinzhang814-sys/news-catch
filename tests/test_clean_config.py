"""Isolated clean-distribution checks; no live model or receiver is contacted."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

IMPORT_DATA=TemporaryDirectory(prefix='news-catch-import-')
os.environ['TREND_DATA_DIR']=IMPORT_DATA.name
os.environ['TREND_DISABLE_SCHEDULER']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'workbench'))
import engine as e


class CleanConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='news-catch-test-')
        self.addCleanup(self.temp.cleanup)
        location=Path(self.temp.name)
        for name,value in [('DATA',location),('DB',location/'trenddesk.sqlite3'),('CONFIG_FILE',location/'config.json')]:
            item=patch.object(e,name,value);item.start();self.addCleanup(item.stop)
        e.init_db()

    def add_ready_article(self):
        article=dict(fingerprint='clean-fixture',title='Test update',body='The product adds a feature.',
                     link='https://example.invalid/news/1',source='Test source',level='A 级',
                     published_at=e.now(),matched_rules=['Test rule'])
        result=e.fallback(article,'test fixture');result['status']='待推送'
        return e.insert_event(article,result,e.datetime.now(e.TZ).date().isoformat())

    def test_first_run_has_no_presets_even_with_inherited_environment(self):
        with patch.dict(os.environ,{'TREND_OPML_PATH':'Z:/missing.opml','TREND_RULES_PATH':'Z:/missing.md',
                                    'TREND_AI_API_KEY':'fixture-inherited','TREND_AI_API_URL':'https://example.invalid',
                                    'TREND_AI_MODEL':'fixture','TREND_WEBHOOK_URL':'https://example.invalid/hook'}):
            config=e.config()
        for key in ('sources','rules','regions','field_mappings'): self.assertEqual(config[key],[])
        self.assertEqual(config['field_map'],{})
        for key in ('ai_url','ai_key','ai_model','webhook_url'): self.assertEqual(config[key],'')
        self.assertEqual(config['time'],'10:00')
        self.assertEqual(e.public_config()['default_field_map'],{})
        self.assertEqual(e.public_config()['field_mappings'],[])

    def test_empty_mapping_migration_and_save_are_idempotent(self):
        e.migrate_webhook_v9();e.migrate_webhook_v9()
        e.save_config(dict(field_mappings=[],ai_model='fixture'))
        self.assertEqual(e.config()['field_mappings'],[])
        self.assertEqual(e.config()['field_map'],{})
        self.assertEqual(e.sql('SELECT count(*) AS n FROM migrations',one=True)['n'],1)
        e.save_config(dict(field_map={}))
        self.assertEqual(e.effective_mappings(e.config()),[])

    def test_user_rule_and_mapping_can_be_added_and_removed(self):
        rule=dict(name='My rule',expression='(voice OR speech) NOT earnings',scope='title_content')
        e.save_config(dict(rules=[rule],regions=['欧洲'],field_mappings=[dict(source='title',target='headline')]))
        self.assertEqual(e.config()['rules'],[rule])
        self.assertTrue(e.match_rules('Product update','Adds speech output',e.config()))
        self.assertFalse(e.match_rules('Earnings update','Adds speech output',e.config()))
        self.assertEqual(e.config()['field_map'],dict(title='headline'))
        e.save_config(dict(rules=[],regions=[],field_mappings=[]))
        self.assertEqual(e.config()['rules'],[])

    def test_invalid_rule_does_not_overwrite_saved_configuration(self):
        baseline=e.config()
        for rule in [dict(name='Empty',scope='title',expression=' '),
                     dict(name='',scope='title',expression='voice'),
                     dict(name='Invalid',scope='title',expression='voice AND')]:
            with self.assertRaises(ValueError): e.save_config(dict(rules=[rule]))
            self.assertEqual(e.config(),baseline)

    def test_no_sources_pipeline_and_preview_are_safe(self):
        with patch.object(e,'request_bytes') as request:
            result=e.run_pipeline()
            self.assertEqual(result['candidate_count'],0)
            self.assertEqual(e.preview()[0],[])
            self.assertEqual(e.push()['status'],'empty')
            request.assert_not_called()

    def test_unmapped_ready_article_cannot_be_previewed_or_sent(self):
        self.add_ready_article()
        e.save_config(dict(webhook_url='https://example.invalid/hook'))
        with patch.object(e,'request_bytes') as request:
            for operation in (e.preview,e.push):
                with self.assertRaisesRegex(ValueError,'至少一个输出字段映射'): operation()
            request.assert_not_called()
        self.assertEqual(e.sql('SELECT count(*) AS n FROM deliveries',one=True)['n'],0)
        self.assertEqual(e.get_events()[0]['status'],'待推送')

    def test_mapping_restores_preview_and_can_be_cleared_without_data_loss(self):
        article_id=self.add_ready_article()
        e.save_config(dict(field_mappings=[dict(source='title',target='headline')]))
        rows,payload=e.preview()
        self.assertEqual(rows[0]['id'],article_id)
        self.assertEqual(set(payload['items'][0]),{'headline'})
        e.save_config(dict(field_mappings=[]))
        self.assertEqual(e.get_events()[0]['id'],article_id)
        with self.assertRaises(ValueError): e.preview()


if __name__=='__main__': unittest.main()
