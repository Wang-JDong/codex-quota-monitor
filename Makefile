.PHONY: test preflight install run dry-run test-notification status postflight enable resource-check rollback

test:
	PYTHONPATH=src python -m pytest

preflight:
	sudo ./deploy/preflight.sh

install:
	sudo ./deploy/install.sh

run:
	sudo systemctl start codex-quota-monitor.service

dry-run:
	sudo ./deploy/dry-run.sh

test-notification:
	sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor test-notification'

status:
	sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor status'

postflight:
	sudo ./deploy/postflight.sh

enable:
	sudo ./deploy/postflight.sh
	./deploy/resource-check.sh
	sudo systemctl enable --now codex-quota-monitor.timer

resource-check:
	./deploy/resource-check.sh

rollback:
	sudo systemctl disable --now codex-quota-monitor.timer 2>/dev/null || true
	sudo systemctl stop codex-quota-monitor.service 2>/dev/null || true
	sudo rm -f /etc/systemd/system/codex-quota-monitor.service /etc/systemd/system/codex-quota-monitor.timer /etc/logrotate.d/codex-quota-monitor
	sudo systemctl daemon-reload
