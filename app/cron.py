import sys, logging
from app.pipeline.run_pipeline import run_pipeline

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

if __name__ == '__main__':
    log.info("Cron started")
    try:
        run_pipeline(triggered_by="cron")
        log.info("Cron finished successfully")
        sys.exit(0)
    except Exception as e:
        log.error(f"Cron failed: {e}")
        sys.exit(1)