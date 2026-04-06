from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the full data pipeline: collect + extract'

    def handle(self, *args, **options):
        self.stdout.write('Starting pipeline...')
        from apps.pipeline.collector import run as collect_run
        collect_run()
        self.stdout.write('Collection done.')
        from apps.pipeline.extractor import run as extract_run
        extract_run()
        self.stdout.write('Extraction done. Pipeline complete.')
