(() => {
  const navButton = document.querySelector('.nav-toggle');
  const nav = document.querySelector('#main-nav');
  if (navButton && nav) {
    navButton.addEventListener('click', () => {
      const open = navButton.getAttribute('aria-expanded') !== 'true';
      navButton.setAttribute('aria-expanded', String(open));
      nav.classList.toggle('is-open', open);
    });
  }


  const tabs = [...document.querySelectorAll('[role="tab"]')];
  for (const tab of tabs) {
    tab.addEventListener('click', () => {
      for (const candidate of tabs) {
        const selected = candidate === tab;
        candidate.setAttribute('aria-selected', String(selected));
        const panel = document.getElementById(candidate.getAttribute('aria-controls'));
        if (panel) panel.hidden = !selected;
      }
    });
  }

  const jobListSentinels = [...document.querySelectorAll('[data-job-list-sentinel]')];
  for (const sentinel of jobListSentinels) {
    const list = sentinel.previousElementSibling;
    const button = sentinel.querySelector('[data-load-more]');
    const loadingLabel = sentinel.querySelector('[data-loading-label]');
    if (!list?.matches('[data-job-list]') || !button) continue;
    let loading = false;
    let observer;

    const loadMore = async () => {
      if (loading || sentinel.hidden) return;
      loading = true;
      sentinel.classList.add('is-loading');
      button.disabled = true;
      try {
        const url = new URL(list.dataset.loadUrl, window.location.origin);
        url.searchParams.set('offset', list.dataset.nextOffset || '0');
        const response = await fetch(url, {
          headers: { Accept: 'text/html' },
          credentials: 'same-origin',
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        list.insertAdjacentHTML('beforeend', await response.text());
        list.dataset.nextOffset = response.headers.get('X-Next-Offset') || list.dataset.nextOffset;
        const hasMore = response.headers.get('X-Has-More') === 'true';
        sentinel.hidden = !hasMore;
        if (!hasMore) observer?.disconnect();
      } catch {
        if (loadingLabel) loadingLabel.hidden = true;
        button.hidden = false;
      } finally {
        loading = false;
        sentinel.classList.remove('is-loading');
        button.disabled = false;
      }
    };

    button.addEventListener('click', loadMore);
    if ('IntersectionObserver' in window) {
      observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMore();
      }, { rootMargin: '500px 0px' });
      observer.observe(sentinel);
    } else {
      button.hidden = false;
      if (loadingLabel) loadingLabel.hidden = true;
    }
  }


  const crawlFilterGroups = [...document.querySelectorAll('[data-crawl-filters]')];
  for (const group of crawlFilterGroups) {
    const panel = group.closest('[role="tabpanel"]') || document;
    const targets = [...panel.querySelectorAll('[data-crawl-target]')];
    const empty = panel.querySelector('[data-crawl-filter-empty]');
    for (const button of group.querySelectorAll('[data-crawl-filter]')) {
      button.addEventListener('click', () => {
        const filter = button.dataset.crawlFilter;
        let visible = 0;
        for (const target of targets) {
          const show = filter === 'all' || target.dataset.crawlTarget === filter;
          target.hidden = !show;
          if (show) visible += 1;
        }
        for (const candidate of group.querySelectorAll('[data-crawl-filter]')) {
          candidate.setAttribute('aria-pressed', String(candidate === button));
        }
        if (empty) empty.hidden = visible > 0 || targets.length === 0;
      });
    }
  }

  const hidePreferenceReview = () => {
    const review = document.querySelector('.preference-review');
    if (review) review.hidden = true;
  };

  const preferenceReview = document.querySelector('[data-preference-review]');
  if (preferenceReview) {
    const focusPreferenceReview = () => {
      const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      window.requestAnimationFrame(() => {
        preferenceReview.focus({ preventScroll: true });
        preferenceReview.scrollIntoView({
          behavior: reduceMotion ? 'auto' : 'smooth',
          block: 'start',
        });
      });
    };
    if (document.readyState === 'complete') {
      focusPreferenceReview();
    } else {
      window.addEventListener('load', focusPreferenceReview, { once: true });
    }
  }

  const bindTextReset = (inputSelector, approvedSelector, buttonSelector, statusSelector) => {
    const input = document.querySelector(inputSelector);
    const approved = document.querySelector(approvedSelector);
    const button = document.querySelector(buttonSelector);
    const status = document.querySelector(statusSelector);
    if (!input || !approved || !button) return;

    const sync = () => {
      button.disabled = input.value === approved.value;
      if (status) status.hidden = true;
    };
    input.addEventListener('input', () => {
      hidePreferenceReview();
      sync();
    });
    button.addEventListener('click', () => {
      input.value = approved.value;
      button.disabled = true;
      hidePreferenceReview();
      const feedback = input.parentElement?.querySelector('.field-feedback');
      if (feedback) feedback.hidden = true;
      input.removeAttribute('aria-invalid');
      if (status) {
        status.textContent = button.dataset.resetConfirmation;
        status.hidden = false;
      }
      input.focus();
    });
    sync();
  };
  bindTextReset(
    '#role_taxonomy',
    '#approved_role_taxonomy',
    '[data-role-taxonomy-reset]',
    '[data-role-taxonomy-reset-status]',
  );
  bindTextReset(
    '#preference_prompt',
    '#approved_preference_prompt',
    '[data-preference-reset]',
    '[data-preference-undo-status]',
  );

  const preferenceInput = document.querySelector('#preference_prompt');
  const preferenceUndo = document.querySelector('[data-preference-undo]');
  const preferenceUndoStatus = document.querySelector('[data-preference-undo-status]');
  if (preferenceInput && preferenceUndo) {
    const snapshots = [];
    let editGroupTimer;

    preferenceInput.addEventListener('beforeinput', () => {
      if (!editGroupTimer) {
        snapshots.push({
          value: preferenceInput.value,
          start: preferenceInput.selectionStart,
          end: preferenceInput.selectionEnd,
        });
        if (snapshots.length > 50) snapshots.shift();
      }
      window.clearTimeout(editGroupTimer);
      editGroupTimer = window.setTimeout(() => {
        editGroupTimer = undefined;
      }, 700);
    });
    preferenceInput.addEventListener('input', () => {
      preferenceUndo.disabled = snapshots.length === 0;
      if (preferenceUndoStatus) preferenceUndoStatus.hidden = true;
      hidePreferenceReview();
    });
    preferenceUndo.addEventListener('click', () => {
      window.clearTimeout(editGroupTimer);
      editGroupTimer = undefined;
      const snapshot = snapshots.pop();
      if (!snapshot) return;
      preferenceInput.value = snapshot.value;
      preferenceInput.setSelectionRange(snapshot.start, snapshot.end);
      preferenceUndo.disabled = snapshots.length === 0;
      hidePreferenceReview();
      if (preferenceUndoStatus) {
        preferenceUndoStatus.textContent = preferenceUndo.dataset.undoConfirmation;
        preferenceUndoStatus.hidden = false;
      }
      preferenceInput.focus();
    });
  }

  const addressLists = [...document.querySelectorAll('[data-address-list]')];
  for (const addressList of addressLists) {
    const rowsContainer = addressList.querySelector('[data-address-rows]');
    const rowTemplate = addressList.querySelector('[data-address-template]');
    const addButton = addressList.querySelector('[data-address-add]');
    const maximum = Number.parseInt(addressList.dataset.maxAddresses || '10', 10);
    const labelTemplate = addressList.dataset.addressLabelTemplate || 'Startadresse __NUMBER__';
    let nextId = addressList.querySelectorAll('[data-address-row]').length;

    if (!rowsContainer || !rowTemplate || !addButton) continue;

    const syncAddressRows = () => {
      const rows = [...rowsContainer.querySelectorAll('[data-address-row]')];
      rows.forEach((row, index) => {
        const input = row.querySelector('input[name="addresses"]');
        const label = row.querySelector('label');
        const removeButton = row.querySelector('[data-address-remove]');
        if (!input) return;
        const accessibleLabel = labelTemplate.replace('__NUMBER__', String(index + 1));
        input.setAttribute('aria-label', accessibleLabel);
        input.autocomplete = index === 0 ? 'street-address' : 'off';
        if (label) {
          label.htmlFor = input.id;
          label.textContent = accessibleLabel;
        }
        if (removeButton) removeButton.hidden = rows.length === 1;
      });
      addButton.disabled = rows.length >= maximum;
    };

    addButton.addEventListener('click', () => {
      if (rowsContainer.querySelectorAll('[data-address-row]').length >= maximum) return;
      const row = rowTemplate.content.firstElementChild?.cloneNode(true);
      if (!row) return;
      const input = row.querySelector('input[name="addresses"]');
      if (!input) return;
      nextId += 1;
      input.id = `address-${nextId}`;
      rowsContainer.append(row);
      syncAddressRows();
      input.focus();
    });

    addressList.addEventListener('click', (event) => {
      const removeButton = event.target.closest('[data-address-remove]');
      if (!removeButton) return;
      const rows = rowsContainer.querySelectorAll('[data-address-row]');
      if (rows.length <= 1) return;
      const row = removeButton.closest('[data-address-row]');
      const nextFocus = row?.previousElementSibling?.querySelector('input')
        || row?.nextElementSibling?.querySelector('input')
        || addButton;
      row?.remove();
      syncAddressRows();
      nextFocus.focus();
    });

    syncAddressRows();
  }

  const cvUpload = document.querySelector('[data-cv-upload]');
  if (cvUpload) {
    const input = cvUpload.querySelector('input[type="file"]');
    const icon = cvUpload.querySelector('[data-cv-icon]');
    const title = cvUpload.querySelector('[data-cv-title]');
    const detail = cvUpload.querySelector('[data-cv-detail]');
    const status = cvUpload.querySelector('[data-cv-status]');
    const action = cvUpload.querySelector('[data-cv-action]');

    input?.addEventListener('change', () => {
      const file = input.files?.[0];
      if (!file) return;
      cvUpload.classList.remove('accepted');
      cvUpload.classList.add('selected');
      cvUpload.dataset.cvState = 'selected';
      if (icon) icon.textContent = '→';
      if (title) title.textContent = cvUpload.dataset.selectedTitle;
      if (detail) {
        detail.textContent = cvUpload.dataset.selectedDetailTemplate.replace(
          '__FILENAME__',
          file.name,
        );
      }
      if (status) status.hidden = true;
      if (action) action.textContent = cvUpload.dataset.selectedAction;
    });
  }

  const saveButton = document.querySelector('[data-save-label]');
  if (saveButton) {
    saveButton.addEventListener('click', () => {
      const saved = saveButton.dataset.saved !== 'true';
      saveButton.dataset.saved = String(saved);
      saveButton.textContent = saved
        ? saveButton.dataset.savedLabel
        : saveButton.dataset.unsavedLabel;
      saveButton.setAttribute('aria-pressed', String(saved));
    });
  }

  const jobPortalChip = document.querySelector('[data-system-job-portals]');
  const companySiteChip = document.querySelector('[data-system-company-sites]');
  const apiCallsChip = document.querySelector('[data-system-api-calls]');
  const chatgptChip = document.querySelector('[data-system-chatgpt]');
  if (jobPortalChip && companySiteChip && apiCallsChip && chatgptChip) {
    const setChipState = (chip, stateClass, labelText) => {
      chip.classList.remove('ready', 'running', 'warning', 'neutral', 'error');
      chip.classList.add(stateClass);
      const label = chip.querySelector('[data-system-label]');
      if (label) label.textContent = labelText;
    };
    const updateSourceChip = (chip, activity) => {
      const meta = chip.querySelector('[data-source-meta]');
      if (!activity) {
        setChipState(chip, 'neutral', chip.dataset.pendingLabel);
        if (meta) meta.hidden = true;
        return;
      }
      const state = activity.state;
      let stateClass = chip.dataset.inactiveClass;
      let labelText = chip.dataset.inactiveLabel;
      if (state === 'running') {
        stateClass = 'running';
        labelText = chip.dataset.runningLabel;
      } else if (state === 'ready') {
        stateClass = 'ready';
        labelText = chip.dataset.activeLabel;
      } else if (state === 'limited') {
        stateClass = 'warning';
        labelText = chip.dataset.limitedLabel;
      } else if (state === 'error') {
        stateClass = 'error';
        labelText = chip.dataset.errorLabel;
      } else if (state === 'pending') {
        stateClass = 'warning';
        labelText = chip.dataset.pendingLabel;
      }
      setChipState(chip, stateClass, labelText);
      if (!meta || !Number.isInteger(activity.jobCount)) return;
      const locale = document.documentElement.lang || 'de';
      const count = new Intl.NumberFormat(locale).format(activity.jobCount);
      const addedLastDay = Number.isInteger(activity.addedLastDay)
        ? activity.addedLastDay
        : 0;
      const added = new Intl.NumberFormat(locale).format(addedLastDay);
      meta.textContent = `${count} ${chip.dataset.foundLabel} · +${added} ${chip.dataset.addedLabel}`;
      meta.hidden = false;
    };
    const updateConnectionChip = (chip, active) => {
      if (active === null) {
        setChipState(chip, 'neutral', chip.dataset.pendingLabel);
        return;
      }
      setChipState(
        chip,
        active ? 'ready' : chip.dataset.inactiveClass,
        active ? chip.dataset.activeLabel : chip.dataset.inactiveLabel,
      );
    };

    const applyStatus = (status) => {
      updateSourceChip(jobPortalChip, status.jobPortals);
      updateSourceChip(companySiteChip, status.companySites);
      updateSourceChip(apiCallsChip, status.apiCalls);
      updateConnectionChip(chatgptChip, status.chatgpt);
    };

    const cacheKey = 'jobradar-system-status-v7';
    let cachedStatus = null;
    try {
      cachedStatus = JSON.parse(sessionStorage.getItem(cacheKey));
    } catch {
      cachedStatus = null;
    }

    const getJson = async (url) => {
      const response = await fetch(url, { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`Status request failed: ${response.status}`);
      return response.json();
    };
    let refreshInFlight = false;
    const refreshStatus = async () => {
      if (refreshInFlight) return;
      refreshInFlight = true;
      try {
        const [healthResult, chatgptResult] = await Promise.allSettled([
          getJson('/api/health'),
          getJson('/api/codex/status'),
        ]);
        const health = healthResult.status === 'fulfilled' ? healthResult.value : null;
        const status = {
          jobPortals: health ? {
            active: health.job_portal_crawling.active === true,
            state: health.job_portal_crawling.state,
            running: health.job_portal_crawling.running === true,
            jobCount: health.job_portal_crawling.job_count,
            addedLastDay: health.job_portal_crawling.added_last_day,
          } : null,
          companySites: health ? {
            active: health.company_site_crawling.active === true,
            state: health.company_site_crawling.state,
            running: health.company_site_crawling.running === true,
            jobCount: health.company_site_crawling.job_count,
            addedLastDay: health.company_site_crawling.added_last_day,
          } : null,
          apiCalls: health ? {
            active: health.api_calls.active === true,
            state: health.api_calls.state,
            running: health.api_calls.running === true,
            jobCount: health.api_calls.job_count,
            addedLastDay: health.api_calls.added_last_day,
          } : null,
          chatgpt: chatgptResult.status === 'fulfilled'
            ? chatgptResult.value.connected === true
            : null,
          checkedAt: Date.now(),
        };
        applyStatus(status);
        try {
          sessionStorage.setItem(cacheKey, JSON.stringify(status));
        } catch {
          // The visible status remains correct when browser storage is unavailable.
        }
      } finally {
        refreshInFlight = false;
      }
    };

    if (
      cachedStatus
      && window.location.search === ''
      && Date.now() - cachedStatus.checkedAt < 60_000
    ) {
      applyStatus(cachedStatus);
    }
    refreshStatus();
    window.setInterval(() => {
      if (!document.hidden) refreshStatus();
    }, 30_000);
  }
  const jobSummary = document.querySelector('[data-job-summary]');
  if (jobSummary?.dataset.summaryState === 'pending') {
    const overview = jobSummary.querySelector('[data-summary-overview]');
    const points = jobSummary.querySelector('[data-summary-points]');
    const missing = jobSummary.querySelector('[data-summary-missing]');
    const errorMessage = jobSummary.querySelector('[data-summary-error]');
    jobSummary.setAttribute('aria-busy', 'true');

    fetch(jobSummary.dataset.summaryUrl, {
      method: 'POST',
      headers: { Accept: 'application/json' },
    }).then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || typeof payload.overview !== 'string') {
        throw new Error(payload.detail || jobSummary.dataset.errorLabel);
      }
      if (overview) overview.textContent = payload.overview;
      if (points) {
        points.replaceChildren(
          ...(payload.key_points || []).map((value) => {
            const item = document.createElement('li');
            item.textContent = value;
            return item;
          }),
        );
        points.hidden = !payload.key_points?.length;
      }
      const missingList = missing?.querySelector('ul');
      if (missing && missingList) {
        missingList.replaceChildren(
          ...(payload.missing_information || []).map((value) => {
            const item = document.createElement('li');
            item.textContent = value;
            return item;
          }),
        );
        missing.hidden = !payload.missing_information?.length;
      }
      jobSummary.dataset.summaryState = 'ready';
    }).catch((requestError) => {
      if (errorMessage) {
        errorMessage.textContent = requestError.message || jobSummary.dataset.errorLabel;
        errorMessage.hidden = false;
      }
    }).finally(() => {
      jobSummary.removeAttribute('aria-busy');
    });
  }
})();
