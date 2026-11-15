/* Executed only on the exact Studio3 page, after the user presses Read.
 * Opens course detail popups only. Never opens a booking/edit form or submits.
 * Returned data excludes account identifiers, reservation IDs and names.
 */
(function () {
  const expectedWeek = __WEEK_JSON__;
  const result = {state: 'reading', completed: 0};
  window.__studio3Read = result;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const tidy = s => String(s || '').replace(/\s+/g, ' ').trim();
  function fail(message) { throw new Error(message); }
  function ownCounts(box, account) {
    const text = tidy(box.textContent);
    if (/Sie haben keine Reservierungen für diese Stunde/.test(text) || text === 'Noch keine Reservierungen') {
      return {own_confirmed: 0, own_waitlist: 0};
    }
    const rows = Array.from(box.querySelectorAll('tr[id^="c"]'));
    if (!rows.length) fail('Eigene Anmeldungen nicht eindeutig lesbar.');
    if (box.querySelector('a[onclick*="getslot"]')) fail('Mehrseitige Anmeldungen bitte im Kursplan prüfen.');
    const headers = Array.from(box.querySelectorAll('tr:first-child > *')).map(el => tidy(el.textContent));
    const createdIndex = headers.indexOf('Erstellt von');
    if (createdIndex < 0) fail('Spalte für eigene Anmeldungen fehlt.');
    let confirmed = 0, waiting = 0;
    for (const row of rows) {
      const cells = Array.from(row.children);
      if (tidy(cells[createdIndex]?.textContent).toLowerCase() !== account.toLowerCase()) {
        fail('Anmeldungen lassen sich nicht sicher dem Konto zuordnen.');
      }
      if (row.querySelector('.wl')) waiting++; else confirmed++;
    }
    return {own_confirmed: confirmed, own_waitlist: waiting};
  }
  (async () => {
    if (location.origin !== 'https://www.supersaas.de' ||
        location.pathname !== '/schedule/Jumping_JAG/Studio_3_Kursplan') fail('Bitte Studio3 öffnen.');
    const login = document.body.innerText.match(/Als\s+(\S+@\S+)\s+angemeldet/);
    if (!login) fail('Bitte zuerst bei Studio3 anmelden.');
    if (!document.querySelector('#container.weekCont')) fail('Bitte Wochenansicht öffnen.');
    const courses = [];
    for (let dayIndex = 0; dayIndex < 7; dayIndex++) {
      const day = new Date(expectedWeek + 'T12:00:00Z'); day.setUTCDate(day.getUTCDate() + dayIndex);
      const iso = day.toISOString().slice(0, 10);
      const header = document.querySelector('#h' + dayIndex);
      const cell = document.querySelector('#d' + dayIndex);
      if (!header || !cell || !new RegExp('(?:^|\\s)' + day.getUTCDate() + '\\.' + (day.getUTCMonth()+1) + '\\.').test(tidy(header.textContent))) {
        fail('Die nächste Kalenderwoche ist noch nicht geladen.');
      }
      for (const chip of cell.querySelectorAll('.chip')) {
        const id = chip.getAttribute('onclick')?.match(/^vs\(event,(\d+)\)$/)?.[1];
        const clock = tidy(chip.querySelector('.head')?.textContent).match(/(\d{1,2}):(\d{2})/);
        const capacity = tidy(chip.querySelector('.chip-title small')?.textContent).match(/^\((\d+)\/(\d+)\)$/);
        const title = tidy(chip.getAttribute('title'));
        if (!id || !clock || !capacity || !title) fail('Kursdaten haben ein unbekanntes Format.');
        courses.push({slot_id: id, day: iso, time: clock[1].padStart(2,'0')+':'+clock[2], title,
          used: Number(capacity[1]), capacity: Number(capacity[2]), status: 'unknown'});
      }
    }
    if (courses.length > 100) fail('Zu viele Termine für eine sichere Einzelprüfung.');
    for (const course of courses) {
      const chip = document.querySelector('[onclick="vs(event,' + course.slot_id + ')"]');
      if (!chip) fail('Kalender hat sich während der Prüfung geändert.');
      chip.click();
      let box = null;
      for (let attempt = 0; attempt < 80; attempt++) {
        await sleep(150);
        box = document.querySelector('#bbox_content');
        const selected = document.querySelector('#bbox_slot_id')?.value;
        if (selected === course.slot_id && box && !box.querySelector('.spin_bg,.spin') && tidy(box.textContent)) break;
        box = null;
      }
      if (!box) fail('Zeitüberschreitung beim Lesen eigener Anmeldungen.');
      Object.assign(course, ownCounts(box, login[1]));
      result.completed++;
      document.querySelector('#bbox a.l-c')?.click();
    }
    Object.assign(result, {state:'done', week_start:expectedWeek, checked_at:new Date().toISOString(), courses});
  })().catch(error => { result.state='error'; result.message=String(error.message || error); });
})();
