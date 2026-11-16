'use strict';

const { Plugin, Notice, Menu, Modal } = require('obsidian');

// Жизненный цикл колонок; на «done» кнопка возвращает задачу в «open» (undo).
const STATUS_ORDER = ['open', 'next', 'in-progress', 'done'];

// Поле в frontmatter задачи: focus (моя задача сейчас) / background (агенты, фон).
const WORK_MODE_KEY = 'workMode';
const WORK_FOCUS = 'focus';
const WORK_BACKGROUND = 'background';
const POMODORO_MINUTES = 25;
const POMODORO_CHOICES = [25, 15, 45, 60];
const TASKS_FOLDER = 'TaskNotes/Tasks/';

// ---------- разбор и правка frontmatter (без YAML-зависимостей) ----------

function readFrontmatterField(text, key) {
  const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!m) return null;
  const line = m[1].match(new RegExp('^' + key + ':[ \\t]*(.*)$', 'm'));
  if (!line) return null;
  let value = line[1].trim();
  if (value.length >= 2) {
    const first = value[0];
    const last = value[value.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      value = value.slice(1, -1);
    }
  }
  return value;
}

function setFrontmatterField(text, key, value) {
  const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!m) {
    if (value == null) return text;
    return '---\n' + key + ': ' + value + '\n---\n' + text;
  }
  const fm = m[1];
  const lineRe = new RegExp('^' + key + ':[^\\n]*\\n?', 'm');
  let next;
  if (value == null) {
    next = fm.replace(lineRe, '');
    next = next.replace(/\n{2,}$/, '\n').replace(/^\n+/, '');
  } else if (lineRe.test(fm)) {
    next = fm.replace(lineRe, key + ': ' + value + '\n');
  } else {
    next = fm.length ? fm + '\n' + key + ': ' + value : key + ': ' + value;
  }
  return '---\n' + next + '\n---' + text.slice(m[0].length);
}

function formatSeconds(total) {
  const s = Math.max(0, Math.round(Number(total) || 0));
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return String(mm).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
}

class AppendTaskModal extends Modal {
  constructor(app, plugin, path, title) {
    super(app);
    this.plugin = plugin;
    this.path = path;
    this.taskTitle = title || path.split('/').pop().replace(/\.md$/, '');
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass('dusk-append-modal');
    contentEl.createEl('h2', { text: 'Дописать к задаче' });
    contentEl.createEl('div', { text: this.taskTitle, cls: 'dusk-append-subject' });
    const ta = contentEl.createEl('textarea', { cls: 'dusk-append-input' });
    ta.rows = 6;
    ta.placeholder = 'Шаги, контекст, ссылки — всё, что нужно помнить по задаче';
    ta.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        this.save();
      }
    });
    this.textarea = ta;
    const row = contentEl.createDiv({ cls: 'dusk-append-actions' });
    const openBtn = row.createEl('button', { text: 'Открыть заметку' });
    openBtn.addEventListener('click', () => {
      this.close();
      this.plugin.openTaskNote(this.path);
    });
    const saveBtn = row.createEl('button', { text: 'Сохранить', cls: 'mod-cta' });
    saveBtn.addEventListener('click', () => this.save());
    window.setTimeout(() => ta.focus(), 0);
  }

  async save() {
    const text = this.textarea.value.trim();
    if (!text) {
      this.close();
      return;
    }
    try {
      await this.plugin.appendToTask(this.path, text);
      new Notice('✍️ Дописано в задачу');
      this.close();
    } catch (err) {
      new Notice('Не удалось дописать: ' + (err && err.message ? err.message : String(err)));
    }
  }

  onClose() {
    this.contentEl.empty();
  }
}

class APlusModal extends Modal {
  constructor(app, plugin, path, title, values) {
    super(app);
    this.plugin = plugin;
    this.path = path;
    this.taskTitle = title || path.split('/').pop().replace(/\.md$/, '');
    this.values = values || {};
  }

  field(label, key, placeholder, rows) {
    const wrap = this.contentEl.createDiv({ cls: 'dusk-aplus-field' });
    wrap.createEl('label', { text: label, cls: 'dusk-aplus-label' });
    const ta = wrap.createEl('textarea', { cls: 'dusk-aplus-input' });
    ta.rows = rows || 2;
    ta.placeholder = placeholder;
    ta.value = this.values[key] || '';
    return ta;
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass('dusk-aplus-modal');
    contentEl.createEl('h2', { text: 'A+ задача' });
    contentEl.createEl('div', { text: this.taskTitle, cls: 'dusk-append-subject' });
    contentEl.createEl('div', {
      text: 'Одно узкое место. Остальное — не сейчас.',
      cls: 'dusk-aplus-hint',
    });
    this.why = this.field('Какое одно узкое место снимает', 'aPlus',
      'Например: парсер без нормального рантайма — блокирует всю ленту', 2);
    this.proof = this.field('Чем докажу прогресс', 'aProof',
      'Наблюдаемый результат: что должно стать правдой', 2);
    this.notDoing = this.field('Что сознательно не делаю', 'aNotDoing',
      'Список того, что отпускаю на этот сезон', 2);
    const row = contentEl.createDiv({ cls: 'dusk-append-actions' });
    const cancel = row.createEl('button', { text: 'Отмена' });
    cancel.addEventListener('click', () => this.close());
    const save = row.createEl('button', { text: 'Сохранить', cls: 'mod-cta' });
    save.addEventListener('click', () => this.save());
    window.setTimeout(() => this.why.focus(), 0);
  }

  async save() {
    try {
      await this.plugin.saveAPlus(this.path, {
        aPlus: this.why.value.trim(),
        aProof: this.proof.value.trim(),
        aNotDoing: this.notDoing.value.trim(),
      });
      new Notice('A+ задача сохранена');
      this.close();
    } catch (err) {
      new Notice('Не удалось сохранить A+: ' + (err && err.message ? err.message : String(err)));
    }
  }

  onClose() {
    this.contentEl.empty();
  }
}

class DuskCardsPlugin extends Plugin {
  async onload() {
    this._decorateTimer = null;
    this._busy = false;

    // карточки перерисовываются постоянно — наблюдаем рабочую область Obsidian,
    // а не весь document.body (тултипы/редактор иначе дают лишние перерисовки)
    const root = (this.app.workspace && this.app.workspace.containerEl) || document.body;
    this.observer = new MutationObserver(() => this.scheduleDecorate());
    this.observer.observe(root, { childList: true, subtree: true });
    this.register(() => this.observer.disconnect());

    // Настройки TaskNotes/статусы могли поменяться — перечитываем бейджи
    this.registerEvent(this.app.metadataCache.on('changed', () => this.scheduleDecorate()));

    // Обратный отсчёт на кнопке активной задачи
    this.registerInterval(window.setInterval(() => this.tick(), 1000));

    this.scheduleDecorate();
  }

  onunload() {
    if (this.observer) this.observer.disconnect();
    document.querySelectorAll('.dusk-advance, .dusk-tools').forEach((el) => el.remove());
  }

  getTaskNotes() {
    const plugins = this.app && this.app.plugins && this.app.plugins.plugins;
    const tn = plugins && plugins['tasknotes'];
    if (!tn || !tn.pomodoroService || !tn.cacheManager) return null;
    return tn;
  }

  getWorkMode(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) return null;
    const cache = this.app.metadataCache.getFileCache(file);
    const fm = cache && cache.frontmatter;
    return fm ? fm[WORK_MODE_KEY] : null;
  }

  scheduleDecorate(force) {
    if (this._decorateTimer && !force) return;
    if (this._decorateTimer) window.clearTimeout(this._decorateTimer);
    this._decorateTimer = window.setTimeout(() => {
      this._decorateTimer = null;
      this.decorate();
    }, 250);
  }

  decorate() {
    const cards = document.querySelectorAll('.kanban-view__card-wrapper');
    for (const card of cards) {
      const path = card.dataset.taskPath;
      if (!path) continue;
      if (!card.querySelector('.dusk-tools')) this.addTools(card, path);
      this.updateCardState(card, path);
    }
  }

  addAdvanceButton(card, path) {
    const btn = document.createElement('button');
    btn.className = 'dusk-advance';
    btn.type = 'button';
    btn.textContent = '→';
    btn.title = 'Список → Дальше → В работе → Готово (на «Готово» — вернуть в «Список»)';
    this.bindNoDrag(btn);
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      e.preventDefault();
      await this.advance(path, btn);
    });
    return btn;
  }

  addTools(card, path) {
    const tools = document.createElement('div');
    tools.className = 'dusk-tools';

    const aplus = document.createElement('button');
    aplus.className = 'dusk-tool dusk-aplus';
    aplus.type = 'button';
    aplus.textContent = 'A+';
    aplus.title = 'A+ задача: узкое место, доказательство, что не делаю';
    this.bindNoDrag(aplus);
    aplus.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      this.openAPlusModal(path);
    });
    tools.appendChild(aplus);

    const append = document.createElement('button');
    append.className = 'dusk-tool dusk-append';
    append.type = 'button';
    append.textContent = '✍️';
    append.title = 'Дописать к задаче: текст, шаги, контекст (Cmd+Enter — сохранить)';
    this.bindNoDrag(append);
    append.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      this.openAppendModal(path);
    });
    tools.appendChild(append);

    const edit = document.createElement('button');
    edit.className = 'dusk-tool dusk-edit';
    edit.type = 'button';
    edit.textContent = '✏️';
    edit.title = 'Поля задачи: статус, приоритет, даты, оценка, подзадачи';
    this.bindNoDrag(edit);
    edit.addEventListener('click', async (e) => {
      e.stopPropagation();
      e.preventDefault();
      await this.runAction(() => this.openTaskEditModal(path));
    });
    tools.appendChild(edit);

    const pomo = document.createElement('button');
    pomo.className = 'dusk-tool dusk-pomo';
    pomo.type = 'button';
    pomo.textContent = '🍅';
    pomo.title = 'Помодоро на эту задачу: выбор 15/25/45/60 (повторный клик — стоп)';
    this.bindNoDrag(pomo);
    pomo.addEventListener('click', async (e) => {
      e.stopPropagation();
      e.preventDefault();
      await this.runAction(() => this.onPomodoroClick(path, e));
    });
    tools.appendChild(pomo);

    const bg = document.createElement('button');
    bg.className = 'dusk-tool dusk-bg';
    bg.type = 'button';
    bg.textContent = '⏳';
    bg.title = 'Фон: агенты/ожидание. Клик — пометить, повторный — снять';
    this.bindNoDrag(bg);
    bg.addEventListener('click', async (e) => {
      e.stopPropagation();
      e.preventDefault();
      await this.runAction(() => this.toggleBackground(path));
    });
    tools.appendChild(bg);
    tools.appendChild(this.addAdvanceButton(card, path));

    card.appendChild(tools);
  }

  bindNoDrag(el) {
    // В Bases-канбане карточка — draggable=true: гасим нативный drag на кнопках,
    // иначе протяжка мышью с зажатой кнопкой начнёт перенос карточки.
    el.draggable = false;
    el.setAttribute('draggable', 'false');
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
    el.addEventListener('dragstart', (e) => e.preventDefault());
  }

  async runAction(fn) {
    if (this._busy) return;
    this._busy = true;
    try {
      await fn();
    } catch (err) {
      console.error('dusk-cards: action failed', err);
      new Notice('Dusk: ошибка — ' + (err && err.message ? err.message : String(err)));
    } finally {
      this._busy = false;
      this.scheduleDecorate(true);
    }
  }

  updateCardState(card, path) {
    const mode = this.getWorkMode(path);
    card.classList.toggle('dusk-focus-card', mode === WORK_FOCUS);
    card.classList.toggle('dusk-background-card', mode === WORK_BACKGROUND);

    const bg = card.querySelector('.dusk-bg');
    if (bg) {
      bg.classList.toggle('is-background', mode === WORK_BACKGROUND);
      bg.title = mode === WORK_BACKGROUND
        ? 'В фоне (агенты/ожидание). Клик — снять метку'
        : 'Пометить как «в фоне» (агенты работают, я не трачу фокус)';
    }
    this.updatePomodoroButton(card, path);
  }

  updatePomodoroButton(card, path) {
    const btn = card.querySelector('.dusk-pomo');
    if (!btn) return;
    const tn = this.getTaskNotes();
    let state = null;
    try {
      state = tn ? tn.pomodoroService.getState() : null;
    } catch (err) {
      state = null;
    }
    const session = state && state.currentSession;
    const isThis = !!(session && session.taskPath === path);
    btn.classList.toggle('is-running', !!(isThis && state.isRunning));
    btn.classList.toggle('is-paused', !!(isThis && !state.isRunning));
    if (isThis) {
      const left = formatSeconds(state.timeRemaining);
      // ставим textContent только при изменении: иначе MutationObserver
      // сам себя будит и крутит decorate несколько раз в секунду
      const next = state.isRunning ? left : '⏸ ' + left;
      if (btn.textContent !== next) btn.textContent = next;
      btn.title = state.isRunning
        ? 'Идёт помодоро на этой задаче — клик, чтобы остановить'
        : 'Помодоро на паузе — клик, чтобы остановить';
    } else {
      if (btn.textContent !== '🍅') btn.textContent = '🍅';
      btn.title = 'Помодоро на эту задачу: 15/25/45/60 минут';
    }
  }

  tick() {
    const cards = document.querySelectorAll('.kanban-view__card-wrapper[data-task-path]');
    for (const card of cards) {
      if (!card.querySelector('.dusk-pomo')) continue;
      this.updatePomodoroButton(card, card.dataset.taskPath);
    }
  }

  openAPlusModal(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    const cache = file ? this.app.metadataCache.getFileCache(file) : null;
    const fm = (cache && cache.frontmatter) || {};
    new APlusModal(this.app, this, path, fm.title || this.taskTitle(path), {
      aPlus: fm.aPlus || '',
      aProof: fm.aProof || '',
      aNotDoing: fm.aNotDoing || '',
    }).open();
  }

  async saveAPlus(path, values) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) throw new Error('задача не найдена');
    await this.app.fileManager.processFrontMatter(file, (fm) => {
      for (const [key, value] of Object.entries(values)) {
        if (value) fm[key] = value;
        else delete fm[key];
      }
    });
    this.scheduleDecorate(true);
  }

  async openNativePomodoroView(path) {
    const tn = this.getTaskNotes();
    if (!tn) return;
    try {
      const right = this.app.workspace.getRightLeaf(false);
      if (right) {
        await right.setViewState({ type: 'tasknotes-pomodoro-view', active: true });
      } else if (typeof tn.activatePomodoroView === 'function') {
        await tn.activatePomodoroView();
      }
    } catch (err) {
      console.error('dusk-cards: open pomodoro view', err);
      try {
        if (typeof tn.activatePomodoroView === 'function') await tn.activatePomodoroView();
      } catch (err2) {
        console.error('dusk-cards: activatePomodoroView fallback', err2);
      }
    }
    const leaves = this.app.workspace.getLeavesOfType('tasknotes-pomodoro-view');
    const view = leaves.length ? leaves[0].view : null;
    if (!view) return;
    try {
      const task = await tn.cacheManager.getTaskInfo(path);
      if (view.selectTask && task) await view.selectTask(task);
      else if (view.updateTaskButtonFromPath) await view.updateTaskButtonFromPath(path);
    } catch (err) {
      console.error('dusk-cards: show task in pomodoro view', err);
    }
  }

  openAppendModal(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    const cache = file ? this.app.metadataCache.getFileCache(file) : null;
    const fm = cache && cache.frontmatter;
    const title = fm && fm.title ? fm.title : this.taskTitle(path);
    new AppendTaskModal(this.app, this, path, title).open();
  }

  openTaskNote(path) {
    this.app.workspace.openLinkText(path, '', false);
  }

  async appendToTask(path, text) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) throw new Error('задача не найдена');
    const source = await this.app.vault.read(file);
    const next = source.replace(/\s+$/, '') + '\n\n' + text.trim() + '\n';
    await this.app.vault.modify(file, next);
  }

  async openTaskEditModal(path) {
    const tn = this.getTaskNotes();
    if (!tn || typeof tn.openTaskEditModal !== 'function') {
      new Notice('TaskNotes не умеет открыть редактор полей — открой заметку');
      return;
    }
    const task = await tn.cacheManager.getTaskInfo(path);
    if (!task) {
      new Notice('TaskNotes пока не видит задачу — обнови борд');
      return;
    }
    await tn.openTaskEditModal(task);
  }

  taskTitle(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (file && file.basename) return file.basename;
    return path ? path.split('/').pop().replace(/\.md$/, '') : '';
  }

  async writeWorkMode(file, value) {
    const text = await this.app.vault.read(file);
    const next = setFrontmatterField(text, WORK_MODE_KEY, value);
    if (next !== text) await this.app.vault.modify(file, next);
  }

  async clearFocusExcept(path) {
    const files = this.app.vault.getMarkdownFiles();
    for (const file of files) {
      if (!file.path.startsWith(TASKS_FOLDER)) continue;
      if (file.path === path) continue;
      const cache = this.app.metadataCache.getFileCache(file);
      const fm = cache && cache.frontmatter;
      if (fm && fm[WORK_MODE_KEY] === WORK_FOCUS) {
        await this.writeWorkMode(file, null);
      }
    }
  }

  async setFocus(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) throw new Error('задача не найдена: ' + path);
    await this.clearFocusExcept(path);
    await this.writeWorkMode(file, WORK_FOCUS);
    this.scheduleDecorate(true);
  }

  async onPomodoroClick(path, evt) {
    const tn = this.getTaskNotes();
    if (!tn) {
      new Notice('TaskNotes не загружен — помодоро недоступен');
      return;
    }
    const state = tn.pomodoroService.getState();
    if (state && state.currentSession) {
      if (state.currentSession.taskPath === path) {
        await tn.pomodoroService.stopPomodoro();
        new Notice('🍅 Помодоро остановлен');
        return;
      }
      const other = state.currentSession.taskPath;
      new Notice('Сначала останови текущий помодоро: ' + (this.taskTitle(other) || 'другая задача'));
      return;
    }
    this.showDurationMenu(path, evt);
  }

  showDurationMenu(path, evt) {
    const menu = new Menu();
    for (const minutes of POMODORO_CHOICES) {
      menu.addItem((item) => {
        item.setTitle('🍅 ' + minutes + ' минут');
        if (minutes === POMODORO_MINUTES) item.setIcon('flame');
        item.onClick(() => this.runAction(() => this.startPomodoro(path, minutes)));
      });
    }
    if (evt) {
      menu.showAtMouseEvent(evt);
    } else {
      menu.showAtPosition({ x: 200, y: 200 });
    }
  }

  async startPomodoro(path, minutes) {
    const tn = this.getTaskNotes();
    if (!tn) {
      new Notice('TaskNotes не загружен — помодоро недоступен');
      return;
    }
    const task = await tn.cacheManager.getTaskInfo(path);
    if (!task) {
      new Notice('TaskNotes пока не видит задачу — обнови борд');
      return;
    }
    await this.setFocus(path);
    await tn.pomodoroService.startPomodoro(task, minutes);
    await this.openNativePomodoroView(path);
    new Notice('🍅 ' + minutes + ' минут: ' + (task.title || this.taskTitle(path)));
  }

  async toggleBackground(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!file) throw new Error('задача не найдена: ' + path);
    const mode = this.getWorkMode(path);
    if (mode === WORK_BACKGROUND) {
      await this.writeWorkMode(file, null);
      new Notice('⏳ Метка «в фоне» снята');
      return;
    }
    // Фоновая задача не должна держать фокус-помодоро.
    const tn = this.getTaskNotes();
    if (tn) {
      const state = tn.pomodoroService.getState();
      if (state && state.currentSession && state.currentSession.taskPath === path) {
        await tn.pomodoroService.stopPomodoro();
      }
    }
    await this.writeWorkMode(file, WORK_BACKGROUND);
    new Notice('⏳ В фоне: агенты/ожидание. Фокус-помодоро не запускается');
  }

  async advance(path, btn) {
    try {
      const file = this.app.vault.getAbstractFileByPath(path);
      if (!file) return;
      const text = await this.app.vault.read(file);
      const m = text.match(/^status:\s*(\S+)\s*$/m);
      const cur = m ? m[1] : 'open';
      const idx = STATUS_ORDER.indexOf(cur);
      const next = STATUS_ORDER[(idx + 1) % STATUS_ORDER.length];
      const updated = m
        ? text.replace(/^status:\s*\S+\s*$/m, 'status: ' + next)
        : text.replace(/^---\s*$/m, '---\nstatus: ' + next, 1);
      await this.app.vault.modify(file, updated);
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = '→'; }, 1200);
    } catch (err) {
      console.error('dusk-cards: advance error', err);
      btn.textContent = '×';
      setTimeout(() => { btn.textContent = '→'; }, 1200);
    }
  }
}

DuskCardsPlugin._test = {
  readFrontmatterField,
  setFrontmatterField,
  formatSeconds,
  WORK_MODE_KEY,
  WORK_FOCUS,
  WORK_BACKGROUND,
  POMODORO_MINUTES,
  POMODORO_CHOICES,
};

module.exports = DuskCardsPlugin;
