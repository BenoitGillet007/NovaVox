const state = { commands: [], listening: false, expandedSynonyms: new Set(), editingIndex: null, listenMode: "always", listenHotkey: null, listenHotkeyAvailable: true, micGateOpen: true, piperVoice: null, profiles: [], activeProfile: null, profileCycleHotkey: null, gameLogHudOverrides: {} };
let micGateMax = 4000; // repli avant chargement, doit correspondre à MIC_GATE_MAX_RAW côté Python

function ready(fn) {
  if (window.pywebview) fn();
  else window.addEventListener("pywebviewready", fn);
}

/* ------------------------------------------------------------ Splash overlay */
// Dessine l'icône "réseau de nœuds" de l'écran de chargement (même
// géométrie que l'ancienne version Tkinter : 6 nœuds en hexagone reliés
// au centre), directement en SVG plutôt qu'en image statique.
function drawSplashIcon() {
  const g = document.getElementById("splashAiNodes");
  if (!g) return;
  const cx = 30, cy = 30, rOuter = 24, rNode = 3.6;
  for (let i = 0; i < 6; i++) {
    const angle = (Math.PI / 180) * (60 * i - 90);
    const nx = cx + rOuter * Math.cos(angle);
    const ny = cy + rOuter * Math.sin(angle);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", cx); line.setAttribute("y1", cy);
    line.setAttribute("x2", nx); line.setAttribute("y2", ny);
    g.appendChild(line);
    const node = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    node.setAttribute("cx", nx); node.setAttribute("cy", ny);
    node.setAttribute("r", rNode);
    node.setAttribute("fill", "#121822");
    g.appendChild(node);
  }
}
drawSplashIcon();

// Appelable depuis Python (window.evaluate_js) pour mettre à jour le
// texte de statut affiché sous le logo pendant le chargement.
function splashSetStatus(text) {
  const el = document.getElementById("splashStatus");
  if (el) el.textContent = text || "";
}

// Fait disparaître l'écran de chargement en fondu. Appelée exclusivement
// depuis Python (voir _run_startup_checks / _replay_startup_log côté
// app.py) une fois la vérification des dépendances réellement terminée —
// pas automatiquement ici en JS dès que get_state() répond, pour éviter
// une course entre les deux : get_state() répond quasiment tout de suite,
// bien avant qu'une éventuelle installation de dépendance manquante ait
// fini côté Python.
//
// Durée minimale d'affichage imposée : sur le chemin de démarrage rapide
// (voir _main_fast_path côté Python), tout peut se charger en quelques
// millisecondes à peine — sans ce minimum, l'écran de chargement pouvait
// apparaître puis disparaître avant même que la fenêtre soit visible à
// l'écran, donnant l'impression qu'il avait complètement disparu.
const SPLASH_MIN_VISIBLE_MS = 3500;
const splashStartTime = Date.now();
let splashHidden = false;
function hideSplashOverlay() {
  if (splashHidden) return;
  const elapsed = Date.now() - splashStartTime;
  if (elapsed < SPLASH_MIN_VISIBLE_MS) {
    setTimeout(hideSplashOverlay, SPLASH_MIN_VISIBLE_MS - elapsed);
    return;
  }
  splashHidden = true;
  const bar2 = document.getElementById("splashBarFill");
  if (bar2) bar2.style.width = "100%";
  const overlay = document.getElementById("splashOverlay");
  // Ne liste les micros qu'une fois les dépendances (sounddevice en
  // particulier) réellement prêtes côté Python — avant ça, l'appel
  // échouerait (sounddevice pas encore importé sur le chemin de
  // démarrage rapide) et produirait une fausse alerte dans le journal
  // alors même que tout finit par fonctionner normalement quelques
  // instants plus tard.
  loadMicDevices();
  if (!overlay) return;
  overlay.classList.add("splash-hidden");
  setTimeout(() => overlay.remove(), 400);
}
// Filet de sécurité : si, pour une raison imprévue, Python n'appelle
// jamais hideSplashOverlay() (bug non anticipé, exception avalée quelque
// part avant d'y arriver...), l'utilisateur ne doit jamais rester bloqué
// indéfiniment derrière l'écran de chargement. Délai volontairement
// généreux pour ne jamais couper une installation de dépendance
// légitimement longue (gros paquet à télécharger, connexion lente...).
setTimeout(hideSplashOverlay, 30000);

ready(init);

async function init() {
  const bar = document.getElementById("splashBarFill");
  if (bar) bar.style.width = "55%";
  applyStoredTheme();
  applyStoredLogVisibility();
  bindEvents();
  try {
    const data = await window.pywebview.api.get_state();
    state.commands = data.commands || [];
    document.getElementById("modelPath").value = data.modelPath || "";
    state.listenMode = data.listenMode || "always";
    state.listenHotkey = data.listenHotkey || null;
    state.listenHotkeyAvailable = data.listenHotkeyAvailable !== false;
    if (data.appVersion) {
      const looksLikeVersion = /^\d/.test(data.appVersion);
      document.getElementById("versionBtn").textContent = looksLikeVersion ? `v${data.appVersion}` : data.appVersion;
    }
    state.profiles = data.profiles || [];
    state.activeProfile = data.activeProfile || null;
    state.profileCycleHotkey = data.profileCycleHotkey || null;
    renderProfiles();
    renderProfileCycleHotkeyUI();
    renderCommands();
    renderListenModeUI();
    if (window.pywebview.api.set_kb_layout) {
      window.pywebview.api.set_kb_layout(kbLayoutChoice);
    }

    if (!data.modelReady) {
      openModelSetup(data.availableVoskModels || []);
    }
  } catch (e) {
    appendLog("[Erreur] Impossible de charger l'état initial.", "error");
  }
  checkForUpdate(); // ne bloque pas le reste de l'init (pas d'await bloquant l'UI)
  setStatus("idle", "Arrêté", "Système en veille");
}

async function checkForUpdate() {
  try {
    const res = await window.pywebview.api.check_for_update();
    if (res && res.available) {
      showUpdateBanner(res.version, res.url);
    }
  } catch (e) {
    // Silencieux : pas de connexion, serveur indisponible... l'appli
    // doit continuer à fonctionner normalement dans tous les cas.
  }
}

function showUpdateBanner(version, url) {
  if (document.getElementById("updateBanner")) return; // déjà affichée
  const banner = document.createElement("div");
  banner.id = "updateBanner";
  banner.className = "update-banner";
  banner.innerHTML = `
    <span>🔔 Nouvelle version disponible : <strong>v${version}</strong></span>
    <div class="update-banner-actions">
      <button type="button" class="btn btn-accent btn-sm" id="updateBannerDownloadBtn">Télécharger</button>
      <button type="button" class="icon-btn" id="updateBannerCloseBtn">✕</button>
    </div>
  `;
  document.body.appendChild(banner);
  document.getElementById("updateBannerDownloadBtn").addEventListener("click", () => {
    window.pywebview.api.open_update_url(url);
  });
  document.getElementById("updateBannerCloseBtn").addEventListener("click", () => {
    banner.remove();
  });
}

function bindEvents() {
  document.getElementById("themeToggle").addEventListener("click", toggleTheme);
  document.getElementById("hideSystemLogToggle").addEventListener("change", onHideSystemLogToggle);
  document.getElementById("openSettingsBtn").addEventListener("click", openSettings);
  document.getElementById("closeSettingsBtn").addEventListener("click", closeSettings);
  document.querySelectorAll(".settings-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchSettingsTab(btn.dataset.settingsTab));
  });
  document.getElementById("settingsModal").addEventListener("click", (e) => {
    if (e.target.id === "settingsModal") closeSettings();
  });
  document.getElementById("versionBtn").addEventListener("click", openPatchNotes);
  document.getElementById("closePatchNotesBtn").addEventListener("click", closePatchNotes);
  document.getElementById("patchNotesModal").addEventListener("click", (e) => {
    if (e.target.id === "patchNotesModal") closePatchNotes();
  });
  document.getElementById("browseBtn").addEventListener("click", browseModel);
  document.getElementById("resetAppBtn").addEventListener("click", onResetApp);
  document.getElementById("exportConfigBtn").addEventListener("click", onExportConfig);
  document.getElementById("importConfigBtn").addEventListener("click", onImportConfig);
  document.getElementById("profileSelect").addEventListener("change", onProfileSwitch);
  document.getElementById("profileNewBtn").addEventListener("click", onProfileNew);
  document.getElementById("profileDuplicateBtn").addEventListener("click", onProfileDuplicate);
  document.getElementById("profileRenameBtn").addEventListener("click", onProfileRename);
  document.getElementById("profileDeleteBtn").addEventListener("click", onProfileDelete);
  document.getElementById("profileCycleHotkeyBtn").addEventListener("click", onOpenProfileCycleHotkey);
  document.getElementById("profileCycleHotkeyClearBtn").addEventListener("click", onClearProfileCycleHotkey);
  document.getElementById("overlayEnabledToggle").addEventListener("change", onOverlayEnabledToggle);
  document.getElementById("autoLaunchWithScToggle").addEventListener("change", onAutoLaunchWithScToggle);
  document.getElementById("overlayEditModeBtn").addEventListener("click", onOverlayEditModeClick);
  document.getElementById("modelSetupBrowseBtn").addEventListener("click", async () => {
    const path = await window.pywebview.api.browse_model();
    if (path) {
      document.getElementById("modelPath").value = path;
      closeModelSetup();
      appendLog(`Modèle sélectionné manuellement : ${path}`, "info");
    }
  });
  document.getElementById("engageBtn").addEventListener("click", toggleListening);
  document.getElementById("micSelect").addEventListener("change", onMicChange);
  document.getElementById("micRefreshBtn").addEventListener("click", loadMicDevices);
  document.getElementById("aecToggle").addEventListener("change", onAecToggleChange);
  document.getElementById("outputSelect").addEventListener("change", onOutputChange);
  document.getElementById("outputRefreshBtn").addEventListener("click", loadOutputDevices);
  document.getElementById("ttsVolumeRange").addEventListener("input", onTtsVolumeInput);
  document.getElementById("ttsVolumeRange").addEventListener("change", onTtsVolumeChange);
  document.getElementById("micGainRange").addEventListener("input", onMicGainInput);
  document.getElementById("micGainRange").addEventListener("change", onMicGainChange);
  document.getElementById("micGateRange").addEventListener("input", onMicGateInput);
  document.getElementById("micGateRange").addEventListener("change", onMicGateChange);
  document.querySelectorAll('input[name="listenMode"]').forEach((radio) => {
    radio.addEventListener("change", onListenModeChange);
  });
  document.getElementById("openListenHotkeyBtn").addEventListener("click", onOpenListenHotkey);
  document.getElementById("captureJoystickBtn").addEventListener("click", onCaptureJoystick);
  document.getElementById("clearListenHotkeyBtn").addEventListener("click", onClearListenHotkey);
  document.getElementById("addForm").addEventListener("submit", onAddCommand);
  document.getElementById("addTitleBtn").addEventListener("click", onAddTitle);
  document.getElementById("clearLog").addEventListener("click", () => {
    document.getElementById("logConsole").innerHTML = "";
  });

  document.getElementById("openKeyboardBtn").addEventListener("click", openKeyboard);
  document.getElementById("closeKeyboardBtn").addEventListener("click", closeKeyboard);
  document.getElementById("kbCancelBtn").addEventListener("click", closeKeyboard);
  document.getElementById("kbClearBtn").addEventListener("click", clearKeyboardSelection);
  document.getElementById("kbConfirmBtn").addEventListener("click", confirmKeyboard);
  document.getElementById("kbCaptureBtn").addEventListener("click", toggleKeyCapture);
  document.getElementById("kbMouseCaptureBtn").addEventListener("click", onCaptureMouseButton);
  document.getElementById("keyboardModal").addEventListener("click", (e) => {
    if (e.target.id === "keyboardModal") closeKeyboard();
  });

  buildVirtualKeyboard();

  document.getElementById("openAiBtn").addEventListener("click", openAiChat);
  document.getElementById("closeAiBtn").addEventListener("click", closeAiChat);
  document.getElementById("aiModal").addEventListener("click", (e) => {
    if (e.target.id === "aiModal") closeAiChat();
  });
  document.getElementById("openGameLogBtn").addEventListener("click", openGameLogEntriesModal);
  document.getElementById("closeGameLogEntriesBtn").addEventListener("click", closeGameLogEntriesModal);
  document.getElementById("gameLogEntriesModal").addEventListener("click", (e) => {
    if (e.target.id === "gameLogEntriesModal") closeGameLogEntriesModal();
  });
  document.getElementById("aiRecheckBtn").addEventListener("click", () => refreshAiStatus(true));
  document.getElementById("aiInstallOllamaBtn").addEventListener("click", startAiInstallOllama);
  document.getElementById("aiPullBtn").addEventListener("click", startAiPull);
  document.getElementById("aiClearBtn").addEventListener("click", clearAiChat);
  document.getElementById("aiRenameBtn").addEventListener("click", renameAi);
  document.getElementById("aiUserNameBtn").addEventListener("click", editUserName);
  document.getElementById("aiVoiceOutputToggle").addEventListener("change", (e) => {
    window.pywebview.api.ai_toggle_voice_output(e.target.checked);
  });
  document.getElementById("aiConfirmCommandsToggle").addEventListener("change", (e) => {
    window.pywebview.api.ai_toggle_confirm_commands(e.target.checked);
  });
  document.getElementById("aiCooldownInput").addEventListener("change", (e) => {
    window.pywebview.api.ai_set_trigger_cooldown(e.target.value);
  });
  document.getElementById("gameLogToggle").addEventListener("change", onGameLogToggle);
  document.getElementById("gameLogAnnounceToggle").addEventListener("change", onGameLogAnnounceToggle);
  document.getElementById("gameLogPlayerHandleSaveBtn").addEventListener("click", onSaveGameLogPlayerHandle);
  document.getElementById("gameLogHudOverrideAddBtn").addEventListener("click", onAddGameLogHudOverride);
  document.getElementById("aiContextInput").addEventListener("input", updateContextCount);
  document.getElementById("aiContextSaveBtn").addEventListener("click", saveAiContext);
  document.getElementById("aiModelSelect").addEventListener("change", async (e) => {
    await window.pywebview.api.ai_set_model(e.target.value);
    updateModelDesc(e.target.value);
    refreshAiStatus();
  });
  document.getElementById("aiUninstallBtn").addEventListener("click", onUninstallModel);
  document.getElementById("aiResponseLengthSelect").addEventListener("change", async (e) => {
    await window.pywebview.api.ai_set_response_length(e.target.value);
  });
  document.getElementById("piperInstallBtn").addEventListener("click", onPiperInstallClick);
  document.getElementById("radioEffectToggle").addEventListener("change", onRadioEffectToggle);
  document.getElementById("piperSpeedRange").addEventListener("input", onPiperSpeedInput);
  document.getElementById("piperSpeedRange").addEventListener("change", onPiperSpeedChange);
  document.getElementById("piperExpressivenessRange").addEventListener("input", onPiperExpressivenessInput);
  document.getElementById("piperExpressivenessRange").addEventListener("change", onPiperExpressivenessChange);
  document.getElementById("kbRepeatToggle").addEventListener("change", (e) => {
  document.getElementById("kbRepeatFields").style.display = e.target.checked ? "flex" : "none";
});
}

/* ------------------------------------------------------------- Thème */

function applyStoredTheme() {
  const saved = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
  document.getElementById("themeToggle").textContent = saved === "dark" ? "🌙" : "☀️";
}

function toggleTheme() {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  document.getElementById("themeToggle").textContent = next === "dark" ? "🌙" : "☀️";
}

/* --------------------------------------------- Journal système (afficher/masquer) */

function applyStoredLogVisibility() {
  const hidden = localStorage.getItem("hideSystemLog") === "1";
  document.querySelector(".log-panel").classList.toggle("hidden", hidden);
  document.querySelector(".main-grid").classList.toggle("log-hidden", hidden);
  const toggle = document.getElementById("hideSystemLogToggle");
  if (toggle) toggle.checked = hidden;
}

function onHideSystemLogToggle(e) {
  localStorage.setItem("hideSystemLog", e.target.checked ? "1" : "0");
  applyStoredLogVisibility();
}

/* --------------------------------------------------------- Commandes */

function moveButtonsHtml(idx) {
  const atTop = idx === 0;
  const atBottom = idx === state.commands.length - 1;
  return `
    <button class="icon-btn-sm move-btn" title="Monter" data-action="move-up" ${atTop ? "disabled" : ""}>▲</button>
    <button class="icon-btn-sm move-btn" title="Descendre" data-action="move-down" ${atBottom ? "disabled" : ""}>▼</button>
  `;
}

// Réordonner les commandes/titres à la souris : clic sur la poignée "⠿"
// d'une carte pour la "prendre en main", puis clic sur une autre carte
// pour la déposer à cet endroit (avant/après selon la moitié cliquée).
// Re-cliquer la même poignée (ou Échap) annule la prise en main.
//
// Choisi délibérément plutôt qu'un glisser-déposer HTML5 natif (bouton de
// souris maintenu) : pendant un glisser natif, la molette ne fonctionne
// plus (le navigateur/WebView2 capture les événements souris autrement
// pendant un drag), rendant impossible d'atteindre une carte hors écran
// sans un défilement automatique en bordure, complexe et jamais aussi
// naturel qu'un vrai défilement à la molette. Ici, comme aucun glisser
// n'est réellement en cours (juste un clic, puis un second clic plus
// tard), la molette reste entièrement libre entre les deux clics.
//
// Écouteurs attachés UNE SEULE FOIS sur le conteneur #commandsList (voir
// l'appel dans renderCommands, protégé par list.dataset.reorderBound),
// avec délégation d'événements vers les cartes individuelles — plus
// robuste qu'un ré-attachement par carte à chaque rendu, puisque les
// cartes sont entièrement recréées (innerHTML="") à chaque appel de
// renderCommands.
let _pickedIndex = null;

function _clearCommandPickVisuals() {
  const list = document.getElementById("commandsList");
  if (!list) return;
  list.classList.remove("picking");
  list.querySelectorAll(".dragging").forEach((el) => el.classList.remove("dragging"));
  list.querySelectorAll(".drag-over-top, .drag-over-bottom").forEach((el) => {
    el.classList.remove("drag-over-top", "drag-over-bottom");
  });
}

// Annule une éventuelle prise en main en cours, sans rien déplacer.
// Exportée au niveau module (pas seulement dans attachCommandReorder) car
// appelée aussi depuis onCardAction — toute AUTRE action sur la liste
// (éditer, supprimer, ▲▼, afficher les synonymes...) doit d'abord annuler
// une prise en main en attente : sans ça, l'index resterait "en main"
// après un changement de la liste (suppression, tri...) qui a pu décaler
// tous les indices, et un clic ultérieur déposerait au mauvais endroit.
function cancelCommandPick() {
  _clearCommandPickVisuals();
  _pickedIndex = null;
}

function attachCommandReorder(list) {
  const CARD_SELECTOR = ".command-card, .command-title-card";

  const clearDropIndicators = () => {
    list.querySelectorAll(".drag-over-top, .drag-over-bottom").forEach((el) => {
      el.classList.remove("drag-over-top", "drag-over-bottom");
    });
  };

  async function dropOnto(card, clientY) {
    const fromIndex = _pickedIndex;
    if (fromIndex === null) return;
    const rect = card.getBoundingClientRect();
    const before = (clientY - rect.top) < rect.height / 2;
    // targetIndex s'interprète comme "insérer avant cette position dans
    // la liste D'ORIGINE" (avant le retrait de l'élément déplacé) — voir
    // Api.move_item_to côté app.py, qui attend précisément ce format.
    let targetIndex = Number(card.dataset.index);
    if (!before) targetIndex += 1;

    cancelCommandPick();

    // Dépose sur soi-même, ou juste après sa propre position actuelle :
    // aucun déplacement réel, on évite l'aller-retour réseau/disque inutile
    // (Api.move_item_to gérerait de toute façon ce cas comme un no-op).
    if (targetIndex === fromIndex || targetIndex === fromIndex + 1) return;

    state.commands = await window.pywebview.api.move_item_to(fromIndex, targetIndex);
    state.editingIndex = null;
    state.expandedSynonyms.clear();
    renderCommands();
  }

  list.addEventListener("click", (e) => {
    const handle = e.target.closest(".drag-handle");
    if (handle) {
      const card = handle.closest(CARD_SELECTOR);
      if (!card) return;
      const idx = Number(card.dataset.index);

      if (_pickedIndex === idx) {
        // Re-clic sur la poignée déjà en main : annule, ne dépose rien.
        cancelCommandPick();
        return;
      }
      if (_pickedIndex === null) {
        _pickedIndex = idx;
        card.classList.add("dragging");
        list.classList.add("picking");
        return;
      }
      // Un autre élément est déjà en main : cliquer sur CETTE poignée
      // vaut dépose ici, comme un clic sur le reste de la carte (voir
      // plus bas) — pas la peine d'exiger un clic "à côté" en plus.
      dropOnto(card, e.clientY);
      return;
    }

    // Clic ailleurs pendant qu'un élément est en main : dépose à cet
    // endroit — sauf sur un vrai bouton d'action (✏️/✕/▲▼/...), qui doit
    // continuer à fonctionner normalement (voir aussi cancelCommandPick
    // appelé en tête d'onCardAction pour ce cas).
    if (_pickedIndex === null) return;
    if (e.target.closest("[data-action]")) return;
    const card = e.target.closest(CARD_SELECTOR);
    if (!card || !list.contains(card)) return;
    dropOnto(card, e.clientY);
  });

  // Survol pendant qu'un élément est en main : indicateur visuel de
  // l'endroit où il sera déposé au prochain clic. Simple mousemove — pas
  // dragover : aucun glisser n'est réellement en cours, la molette reste
  // donc utilisable normalement entre les deux clics.
  list.addEventListener("mousemove", (e) => {
    if (_pickedIndex === null) return;
    const card = e.target.closest(CARD_SELECTOR);
    if (!card || !list.contains(card) || Number(card.dataset.index) === _pickedIndex) {
      clearDropIndicators();
      return;
    }
    clearDropIndicators();
    const rect = card.getBoundingClientRect();
    const before = (e.clientY - rect.top) < rect.height / 2;
    card.classList.add(before ? "drag-over-top" : "drag-over-bottom");
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && _pickedIndex !== null) cancelCommandPick();
  });
}

function renderCommands() {
  const list = document.getElementById("commandsList");
  // Filet de sécurité : toute autre cause de re-rendu de la liste (ajout
  // de commande, changement de profil, import de configuration...) qui
  // ne passe pas par onCardAction (qui annule déjà la prise en main) doit
  // quand même invalider un éventuel index "en main" resté périmé —
  // sinon un clic ultérieur pourrait déposer au mauvais endroit après
  // que les indices ont changé sous ses pieds.
  if (_pickedIndex !== null) cancelCommandPick();
  list.innerHTML = "";
  const cmdOnlyCount = state.commands.filter((it) => it.type !== "title").length;
  document.getElementById("cmdCount").textContent = cmdOnlyCount;

  // Réordonnement clic-clic (voir attachCommandReorder) : attaché UNE
  // SEULE FOIS sur le conteneur persistant (#commandsList n'est jamais
  // recréé, seul son contenu l'est à chaque renderCommands via
  // innerHTML="") — un dataset-flag évite de réattacher les mêmes
  // écouteurs à chaque rendu, ce qui les aurait sinon empilés en
  // double/triple au fil des rendus successifs.
  if (!list.dataset.reorderBound) {
    attachCommandReorder(list);
    list.dataset.reorderBound = "1";
  }

  state.commands.forEach((item, idx) => {
    if (item.type === "title") {
      const card = document.createElement("article");
      card.className = "command-title-card";
      card.dataset.index = String(idx);
      card.innerHTML = `
        <div class="command-title-left">
          <span class="drag-handle" title="Cliquer pour prendre cette ligne, puis cliquer où la déposer">⠿</span>
          <span class="command-title-text">${escapeHtml(item.text)}</span>
        </div>
        <div class="command-actions">
          ${moveButtonsHtml(idx)}
          <button class="icon-btn-sm" title="Modifier le titre" data-action="edit-title">✏️</button>
          <button class="icon-btn-sm danger" title="Supprimer le titre" data-action="delete-title">✕</button>
        </div>
      `;
      list.appendChild(card);
      return;
    }

    const cmd = item;
    const card = document.createElement("article");
    card.className = "command-card";
    card.dataset.index = String(idx);

    const hasSynonyms = cmd.synonyms && cmd.synonyms.length > 0;
    const isEditing = state.editingIndex === idx;

    const main = document.createElement("div");
    main.className = "command-main";
    const keysDisplay = String(cmd.keys || "")
      .split("+")
      .map((k) => keyDisplayLabel(k.trim().toLowerCase()))
      .join(" + ");

    if (isEditing) {
      card.classList.add("editing");
      main.innerHTML = `
        <input type="text" class="edit-phrase-input" data-action="noop" value="${escapeHtml(cmd.phrase)}" placeholder="Phrase à dire" />
        <button type="button" class="keycap keycap-edit" data-action="edit-key" data-keys="${escapeHtml(cmd.keys)}" data-hold="${cmd.hold ? "1" : "0"}" data-repeat-count="${cmd.repeat_count || 1}" data-repeat-delay="${cmd.repeat_delay ?? 0.1}" title="Cliquer pour changer la touche">${escapeHtml(keysDisplay)}</button>
        <div class="command-actions">
          <button class="icon-btn-sm accent" title="Enregistrer" data-action="save-edit">✓ Valider</button>
          <button class="icon-btn-sm" title="Annuler" data-action="cancel-edit">Annuler</button>
        </div>
      `;
    } else {
      main.innerHTML = `
        <span class="drag-handle" title="Cliquer pour prendre cette ligne, puis cliquer où la déposer">⠿</span>
        <span class="keycap" title="${escapeHtml(cmd.keys)}">${escapeHtml(keysDisplay)}${cmd.hold ? ' <span class="hold-badge" title="Touche maintenue 2 secondes">⏱</span>' : ""}${(cmd.repeat_count && cmd.repeat_count > 1) ? ` <span class="hold-badge" title="Répétée ${cmd.repeat_count} fois, délai ${cmd.repeat_delay ?? 0.1}s">🔁${cmd.repeat_count}</span>` : ""}</span>
        <span class="command-phrase">${escapeHtml(cmd.phrase)}</span>
        ${hasSynonyms ? `
          <button class="icon-btn-sm toggle-syn" title="Afficher/masquer les synonymes" data-action="toggle-syn">
            <span class="toggle-arrow">▸</span> ${cmd.synonyms.length} syn
          </button>
        ` : ""}
        <div class="command-actions">
          ${moveButtonsHtml(idx)}
          <button class="icon-btn-sm" title="Ajouter un synonyme" data-action="add-syn">+ syn</button>
          <button class="icon-btn-sm" title="Modifier la phrase / la touche" data-action="edit">✏️</button>
          <button class="icon-btn-sm danger" title="Supprimer" data-action="delete">✕</button>
        </div>
      `;
    }
    card.appendChild(main);

    if (hasSynonyms) {
      const ul = document.createElement("ul");
      const isExpanded = isEditing || state.expandedSynonyms.has(idx);
      ul.className = "synonyms-list" + (isExpanded ? "" : " collapsed");
      cmd.synonyms.forEach((syn, sIdx) => {
        const li = document.createElement("li");
        li.innerHTML = `<span>${escapeHtml(syn)}</span><span class="syn-actions"><button class="icon-btn-sm" data-action="edit-syn" data-syn="${sIdx}">✏️</button><button class="icon-btn-sm danger" data-action="delete-syn" data-syn="${sIdx}">✕</button></span>`;
        ul.appendChild(li);
      });
      card.appendChild(ul);

      const arrow = main.querySelector(".toggle-arrow");
      if (arrow) arrow.textContent = isExpanded ? "▾" : "▸";
    }

    list.appendChild(card);
  });

  list.querySelectorAll("[data-action]").forEach((btn) => {
    if (btn.dataset.action === "noop") return;
    btn.addEventListener("click", onCardAction);
  });

  if (state.editingIndex !== null) {
    const input = list.querySelector(`.command-card[data-index="${state.editingIndex}"] .edit-phrase-input`);
    if (input) {
      input.focus();
      input.select();
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          saveEditedCommand(state.editingIndex);
        } else if (e.key === "Escape") {
          e.preventDefault();
          state.editingIndex = null;
          renderCommands();
        }
      });
    }
  }
}

async function saveEditedCommand(idx) {
  const card = document.querySelector(`.command-card[data-index="${idx}"]`);
  if (!card) return;
  const phraseInput = card.querySelector(".edit-phrase-input");
  const keyBtn = card.querySelector(".keycap-edit");
  const phrase = phraseInput.value.trim();
  const keys = (keyBtn.dataset.keys || "").trim().toLowerCase();
  if (!phrase || !keys) return;
  const hold = keyBtn.dataset.hold === "1";
  const repeatCount = parseInt(keyBtn.dataset.repeatCount || "1", 10) || 1;
  const repeatDelay = parseFloat(keyBtn.dataset.repeatDelay || "0.1") || 0.1;

  state.commands = await window.pywebview.api.edit_command(idx, phrase, keys, hold, repeatCount, repeatDelay);
  state.editingIndex = null;
  renderCommands();
}

async function onCardAction(e) {
  const btn = e.currentTarget;
  const card = btn.closest(".command-card, .command-title-card");
  const idx = Number(card.dataset.index);
  const action = btn.dataset.action;

  // Toute action réelle sur la liste (éditer, supprimer, ▲▼, synonymes...)
  // annule une éventuelle prise en main en attente (voir attachCommandReorder) :
  // sans ça, un index "en main" laissé de côté pourrait pointer sur le
  // mauvais élément après un changement de la liste (suppression, tri...),
  // et un clic ultérieur déposerait au mauvais endroit.
  cancelCommandPick();

  if (action === "move-up") {
    state.commands = await window.pywebview.api.move_item(idx, "up");
    state.editingIndex = null;
    state.expandedSynonyms.clear();
    renderCommands();
  } else if (action === "move-down") {
    state.commands = await window.pywebview.api.move_item(idx, "down");
    state.editingIndex = null;
    state.expandedSynonyms.clear();
    renderCommands();
  } else if (action === "edit-title") {
    const current = state.commands[idx].text;
    const updated = prompt("Modifier le titre :", current);
    if (updated && updated.trim() && updated.trim() !== current) {
      state.commands = await window.pywebview.api.edit_title(idx, updated.trim());
      renderCommands();
    }
  } else if (action === "delete-title") {
    const text = state.commands[idx].text;
    const confirmed = confirm(`Supprimer le titre « ${text} » ? (les commandes en dessous ne sont pas supprimées)`);
    if (!confirmed) return;
    state.commands = await window.pywebview.api.delete_command(idx);
    renderCommands();
  } else if (action === "delete") {
    const phrase = state.commands[idx].phrase;
    const confirmed = confirm(`Supprimer la commande « ${phrase} » et tous ses synonymes ?`);
    if (!confirmed) return;
    state.commands = await window.pywebview.api.delete_command(idx);
    state.expandedSynonyms.delete(idx);
    renderCommands();
  } else if (action === "delete-syn") {
    const synIdx = Number(btn.dataset.syn);
    const synonyme = state.commands[idx].synonyms[synIdx];
    const confirmed = confirm(`Supprimer le synonyme « ${synonyme} » ?`);
    if (!confirmed) return;
    state.commands = await window.pywebview.api.delete_synonym(idx, synIdx);
    renderCommands();
  } else if (action === "add-syn") {
    const syn = prompt(`Texte mal entendu à associer à « ${state.commands[idx].phrase} » :`);
    if (syn && syn.trim()) {
      state.commands = await window.pywebview.api.add_synonym(idx, syn.trim());
      renderCommands();
    }
  } else if (action === "edit-syn") {
    const synIdx = Number(btn.dataset.syn);
    const current = state.commands[idx].synonyms[synIdx];
    const updated = prompt(`Modifier le synonyme de « ${state.commands[idx].phrase} » :`, current);
    if (updated && updated.trim() && updated.trim().toLowerCase() !== current) {
      state.commands = await window.pywebview.api.edit_synonym(idx, synIdx, updated.trim());
      renderCommands();
    }
  } else if (action === "edit") {
    state.editingIndex = idx;
    state.expandedSynonyms.add(idx);
    renderCommands();
} else if (action === "edit-key") {
    openKeyboard((combo, holdChecked, repeatCount, repeatDelay) => {
      btn.dataset.keys = combo;
      btn.dataset.hold = holdChecked ? "1" : "0";
      btn.dataset.repeatCount = String(repeatCount);
      btn.dataset.repeatDelay = String(repeatDelay);
      const display = combo
        .split("+")
        .map((k) => keyDisplayLabel(k.trim().toLowerCase()))
        .join(" + ");
      btn.textContent = display;
    }, btn.dataset.keys, {
      holdSeed: btn.dataset.hold === "1",
      repeatSeed: Number(btn.dataset.repeatCount || 1) > 1,
      repeatCountSeed: Number(btn.dataset.repeatCount || 3),
      repeatDelaySeed: Number(btn.dataset.repeatDelay || 0.1),
    });
  } else if (action === "save-edit") {
    await saveEditedCommand(idx);
  } else if (action === "cancel-edit") {
    state.editingIndex = null;
    renderCommands();
  } else if (action === "toggle-syn") {
    const ul = card.querySelector(".synonyms-list");
    const arrow = btn.querySelector(".toggle-arrow");
    const isCollapsed = ul.classList.toggle("collapsed");
    if (arrow) arrow.textContent = isCollapsed ? "▸" : "▾";
    if (isCollapsed) {
      state.expandedSynonyms.delete(idx);
    } else {
      state.expandedSynonyms.add(idx);
    }
  }
}

async function onAddCommand(e) {
  e.preventDefault();
  const phraseInput = document.getElementById("phraseInput");
  const keysInput = document.getElementById("keysInput");
  const phrase = phraseInput.value.trim();
  const keys = keysInput.value.trim().toLowerCase();
  if (!phrase || !keys) return;
  const hold = keysInput.dataset.hold === "1";
  const repeatCount = parseInt(keysInput.dataset.repeatCount || "1", 10) || 1;
  const repeatDelay = parseFloat(keysInput.dataset.repeatDelay || "0.1") || 0.1;

  state.commands = await window.pywebview.api.add_command(phrase, keys, hold, repeatCount, repeatDelay);
  renderCommands();
  phraseInput.value = "";
  keysInput.value = "";
  keysInput.dataset.hold = "0";
  keysInput.dataset.repeatCount = "1";
  keysInput.dataset.repeatDelay = "0.1";
  phraseInput.focus();
}

async function onAddTitle() {
  const text = prompt("Texte du titre à ajouter (ex. « Bouclier ») :");
  if (text && text.trim()) {
    state.commands = await window.pywebview.api.add_title(text.trim());
    renderCommands();
  }
}

function openSettings() {
  document.getElementById("settingsModal").classList.remove("hidden");
  switchSettingsTab("sons");
  loadMicDevices();
  loadOutputDevices();
  if (!state.listening) {
    window.pywebview.api.start_mic_monitor();
  }
  refreshOverlayUI();
  refreshAutoLaunchUI();
}

function closeSettings() {
  document.getElementById("settingsModal").classList.add("hidden");
  window.pywebview.api.stop_mic_monitor();
}

function switchSettingsTab(tabName) {
  document.querySelectorAll(".settings-tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.settingsTab === tabName);
  });
  document.querySelectorAll(".settings-tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.settingsPanel !== tabName);
  });
  if (tabName === "ia") {
    loadAiSettingsTab();
  } else if (tabName === "gamelog") {
    loadGameLogSettingsTab();
  }
}

/* ------------------------------------------------------------------ Overlay */

async function refreshOverlayUI() {
  try {
    const overlayState = await window.pywebview.api.overlay_get_state();
    document.getElementById("overlayEnabledToggle").checked = !!overlayState.enabled;
    document.getElementById("overlayEditRow").style.display = overlayState.enabled ? "flex" : "none";
    setOverlayEditButtonState(!!overlayState.editMode);
  } catch (e) {
    // état overlay indisponible (ex. dépendances pas encore prêtes) : ignore
  }
}

async function refreshAutoLaunchUI() {
  try {
    const enabled = await window.pywebview.api.get_autolaunch_with_sc_enabled();
    document.getElementById("autoLaunchWithScToggle").checked = !!enabled;
  } catch (e) {
    // indisponible (ex. hors Windows) : ignore
  }
}

async function onAutoLaunchWithScToggle(e) {
  const desired = e.target.checked;
  const ok = await window.pywebview.api.set_autolaunch_with_sc_enabled(desired);
  if (desired && !ok) {
    e.target.checked = false;
    appendLog("Ce réglage n'est disponible que depuis la version installée (.exe), pas en développement.", "warn");
    return;
  }
  appendLog(
    desired
      ? "NovaVox se lancera automatiquement au démarrage de Star Citizen."
      : "Lancement automatique désactivé.",
    "info"
  );
}

function setOverlayEditButtonState(editable) {
  const btn = document.getElementById("overlayEditModeBtn");
  if (!btn) return;
  btn.textContent = editable ? "🔒 Verrouiller l'overlay" : "🔓 Déplacer l'overlay";
  btn.classList.toggle("active", editable);
}

async function onOverlayEnabledToggle(e) {
  const res = await window.pywebview.api.overlay_set_enabled(e.target.checked);
  document.getElementById("overlayEditRow").style.display = res.enabled ? "flex" : "none";
  setOverlayEditButtonState(!!res.editMode);
  appendLog(res.enabled ? "Overlay en jeu activé." : "Overlay en jeu désactivé.", "info");
}

async function onOverlayEditModeClick() {
  const currentlyEditable = document.getElementById("overlayEditModeBtn").classList.contains("active");
  const editable = await window.pywebview.api.overlay_set_edit_mode(!currentlyEditable);
  setOverlayEditButtonState(editable);
  appendLog(
    editable
      ? "Overlay déverrouillé : fais-le glisser où tu veux, puis reclique pour verrouiller."
      : "Overlay verrouillé (clics traversants, position enregistrée).",
    "info"
  );
}

/* ------------------------------------------------------------- Notes MAJ */

// Découpe le texte brut de patch_maj.txt en blocs par version. Repère
// chaque ligne "vX.Y[.Z]" (même format que la regex utilisée côté Python
// pour déterminer le numéro de version courant — voir get_app_version
// dans app.py) et ignore les lignes de séparation "----" ainsi que
// l'en-tête libre avant la première version.
function parsePatchNotes(text) {
  const lines = (text || "").split(/\r?\n/);
  const versions = [];
  let current = null;
  let lastTopItem = null; // dernier élément de premier niveau, pour y rattacher des sous-puces
  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    const versionMatch = trimmed.match(/^v(\d+\.\d+(?:\.\d+)?)\s*$/);
    if (versionMatch) {
      current = { version: versionMatch[1], items: [] };
      versions.push(current);
      lastTopItem = null;
      continue;
    }
    if (!current) continue; // texte avant la première version (titre, etc.) : ignoré
    if (!trimmed || /^[=\-]{3,}$/.test(trimmed)) continue; // ligne vide, séparateur "----" ou "===="

    // Le niveau d'indentation se mesure AVANT de retirer les espaces
    // (rawLine, pas trimmed) : c'est ce qui permet de distinguer une
    // puce de premier niveau ("- Texte") d'une sous-puce indentée
    // ("      -Texte").
    const bulletMatch = rawLine.match(/^(\s*)[-*•]\s*(.*)$/);
    if (bulletMatch) {
      const indent = bulletMatch[1].length;
      const itemText = bulletMatch[2];
      if (indent >= 3 && lastTopItem) {
        // Sous-puce : rattachée au dernier élément de premier niveau.
        lastTopItem.subs.push(itemText);
      } else {
        // Puce de premier niveau : nouvel élément.
        const item = { text: itemText, subs: [] };
        current.items.push(item);
        lastTopItem = item;
      }
      continue;
    }

    // Ligne de continuation (pas de tiret en tête, ex. retour à la ligne
    // dans le fichier pour une puce trop longue) : rattachée à la
    // dernière sous-puce si on est dans un bloc de sous-puces, sinon à
    // la dernière puce de premier niveau.
    if (lastTopItem) {
      if (lastTopItem.subs.length) {
        lastTopItem.subs[lastTopItem.subs.length - 1] += " " + trimmed;
      } else {
        lastTopItem.text += " " + trimmed;
      }
    }
  }
  return versions;
}

function renderPatchNotes(text) {
  const container = document.getElementById("patchNotesContent");
  container.innerHTML = "";
  const versions = parsePatchNotes(text);

  if (!versions.length) {
    const p = document.createElement("p");
    p.textContent = text && text.trim() ? text : "Aucune note de mise à jour disponible.";
    container.appendChild(p);
    return;
  }

  versions.forEach((v, index) => {
    const isLatest = index === 0;
    const block = document.createElement("div");
    block.className = "patch-note-version " + (isLatest ? "is-latest" : "is-old");

    const header = document.createElement("div");
    header.className = "patch-note-header";
    const num = document.createElement("span");
    num.className = "patch-note-version-num";
    num.textContent = `v${v.version}`;
    header.appendChild(num);
    if (isLatest) {
      const tag = document.createElement("span");
      tag.className = "patch-note-latest-tag";
      tag.textContent = "Actuelle";
      header.appendChild(tag);
    }
    block.appendChild(header);

    if (v.items.length) {
      const list = document.createElement("ul");
      list.className = "patch-note-list";
      for (const item of v.items) {
        const li = document.createElement("li");
        li.textContent = item.text;
        if (item.subs && item.subs.length) {
          // Styles posés en ligne (pas dans style.css, non disponible
          // ici) : sous-liste visuellement distincte — légèrement
          // indentée, séparée par une barre verticale discrète, puces
          // plus petites et un peu atténuées par rapport au niveau
          // principal.
          const subList = document.createElement("ul");
          subList.className = "patch-note-sublist";
          subList.style.marginTop = "4px";
          subList.style.marginBottom = "2px";
          subList.style.marginLeft = "10px";
          subList.style.paddingLeft = "14px";
          subList.style.borderLeft = "2px solid rgba(255,255,255,0.15)";
          subList.style.listStyleType = "circle";
          subList.style.opacity = "0.82";
          subList.style.fontSize = "0.94em";
          for (const sub of item.subs) {
            const subLi = document.createElement("li");
            subLi.textContent = sub;
            subLi.style.marginTop = "2px";
            subList.appendChild(subLi);
          }
          li.appendChild(subList);
        }
        list.appendChild(li);
      }
      block.appendChild(list);
    }

    container.appendChild(block);
  });
}

async function openPatchNotes() {
  document.getElementById("patchNotesModal").classList.remove("hidden");
  const content = document.getElementById("patchNotesContent");
  content.innerHTML = "";
  const loading = document.createElement("p");
  loading.textContent = "Chargement...";
  content.appendChild(loading);
  try {
    const text = await window.pywebview.api.get_patch_notes();
    renderPatchNotes(text);
  } catch (e) {
    content.innerHTML = "";
    const err = document.createElement("p");
    err.textContent = "Impossible de charger les notes de mise à jour.";
    content.appendChild(err);
  }
}

function closePatchNotes() {
  document.getElementById("patchNotesModal").classList.add("hidden");
}

/* ------------------------------------------------------------ Modèle */

async function browseModel() {
  const path = await window.pywebview.api.browse_model();
  if (path) document.getElementById("modelPath").value = path;
}

async function onResetApp() {
  const removeOllama = document.getElementById("resetRemoveOllamaCheck").checked;
  const btn = document.getElementById("resetAppBtn");

  const detail = removeOllama
    ? "le modèle vocal téléchargé, tes commandes/réglages personnalisés, les journaux, le raccourci Bureau, ET Ollama avec ses modèles IA"
    : "le modèle vocal téléchargé, tes commandes/réglages personnalisés, les journaux et le raccourci Bureau (Ollama ne sera pas touché)";

  if (!confirm(`Ceci va supprimer ${detail}, remettre l'application dans son état de départ, et la fermer.\n\nCette action est IRRÉVERSIBLE. Continuer ?`)) {
    return;
  }

  btn.disabled = true;
  btn.textContent = "Réinitialisation en cours...";
  appendLog("Réinitialisation de l'application demandée par l'utilisateur...", "info");

  try {
    await window.pywebview.api.reset_application(removeOllama);
    // L'interface se recharge d'elle-même une fois la réinitialisation
    // terminée côté Python (voir _reset_application_thread) : rien
    // d'autre à faire ici.
  } catch (e) {
    appendLog("[Erreur] La réinitialisation n'a pas pu démarrer.", "error");
    btn.disabled = false;
    btn.textContent = "🔄 Réinitialiser l'application";
  }
}

/* ------------------------------------------------------------ Profils */

function renderProfiles() {
  const select = document.getElementById("profileSelect");
  select.innerHTML = "";
  // Repère les noms utilisés plusieurs fois : le nombre de commandes est
  // affiché seulement pour ceux-là, pour aider à distinguer des doublons
  // (ex. après une ancienne duplication) sans alourdir l'affichage quand
  // chaque profil a déjà un nom unique.
  const nameCounts = {};
  state.profiles.forEach((p) => {
    const key = p.name.toLowerCase();
    nameCounts[key] = (nameCounts[key] || 0) + 1;
  });
  state.profiles.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.id;
    const isDuplicateName = nameCounts[p.name.toLowerCase()] > 1;
    opt.textContent = isDuplicateName ? `${p.name} (${p.count} commande${p.count === 1 ? "" : "s"})` : p.name;
    if (p.id === state.activeProfile) opt.selected = true;
    select.appendChild(opt);
  });
  // Supprimer/dupliquer n'ont de sens que s'il existe plus d'un profil
  // à conserver après coup, ou un profil actif à copier.
  document.getElementById("profileDeleteBtn").disabled = state.profiles.length <= 1;
}

function renderProfileCycleHotkeyUI() {
  const label = document.getElementById("profileCycleHotkeyLabel");
  label.textContent = state.profileCycleHotkey
    ? state.profileCycleHotkey.toUpperCase()
    : "Non défini";
}

function onOpenProfileCycleHotkey() {
  openKeyboard(async (combo) => {
    const res = await window.pywebview.api.set_profile_cycle_hotkey(combo);
    state.profileCycleHotkey = res.hotkey || null;
    renderProfileCycleHotkeyUI();
  }, state.profileCycleHotkey || "", { showHold: false });
}

async function onClearProfileCycleHotkey() {
  const res = await window.pywebview.api.set_profile_cycle_hotkey("");
  state.profileCycleHotkey = res.hotkey || null;
  renderProfileCycleHotkeyUI();
}

// Appelé depuis Python (voir Api._cycle_profile) quand le profil change
// suite au raccourci clavier global — pas via un clic dans l'interface,
// donc rien ne synchronise l'état côté JS sans ce point d'entrée dédié.
function profileSwitchedExternally(name, profileId, commands) {
  state.activeProfile = profileId;
  state.commands = commands || [];
  renderProfiles();
  renderCommands();
  appendLog(`Profil actif (raccourci clavier) : « ${name} ».`, "info");
}

async function onProfileSwitch(e) {
  const profileId = e.target.value;
  if (!profileId || profileId === state.activeProfile) return;
  try {
    const result = await window.pywebview.api.profiles_switch(profileId);
    if (result && result.ok) {
      state.activeProfile = profileId;
      state.commands = result.commands || [];
      renderCommands();
      appendLog(`Profil actif : « ${result.name} ».`, "info");
    } else {
      appendLog(`[Erreur] Changement de profil : ${(result && result.error) || "échec inconnu"}.`, "error");
      renderProfiles(); // remet le select sur le profil réellement actif
    }
  } catch (err) {
    appendLog("[Erreur] Changement de profil impossible.", "error");
    renderProfiles();
  }
}

async function onProfileNew() {
  const name = prompt("Nom du nouveau profil (vide) :");
  if (!name || !name.trim()) return;
  try {
    const result = await window.pywebview.api.profiles_create(name.trim(), false);
    if (result && result.ok) {
      state.profiles = result.profiles || [];
      state.activeProfile = result.id;
      state.commands = result.commands || [];
      renderProfiles();
      renderCommands();
      appendLog(`Profil « ${result.name} » créé et activé.`, "success");
    } else {
      appendLog(`[Erreur] Création du profil : ${(result && result.error) || "échec inconnu"}.`, "error");
    }
  } catch (err) {
    appendLog("[Erreur] Création du profil impossible.", "error");
  }
}

async function onProfileDuplicate() {
  const currentOpt = document.getElementById("profileSelect").selectedOptions[0];
  const currentName = currentOpt ? currentOpt.textContent : "";
  const name = prompt("Nom du nouveau profil (copie du profil actif) :", `${currentName} (copie)`);
  if (!name || !name.trim()) return;
  try {
    const result = await window.pywebview.api.profiles_create(name.trim(), true);
    if (result && result.ok) {
      state.profiles = result.profiles || [];
      state.activeProfile = result.id;
      state.commands = result.commands || [];
      renderProfiles();
      renderCommands();
      appendLog(`Profil « ${result.name} » créé (copie de « ${currentName} ») et activé.`, "success");
    } else {
      appendLog(`[Erreur] Duplication du profil : ${(result && result.error) || "échec inconnu"}.`, "error");
    }
  } catch (err) {
    appendLog("[Erreur] Duplication du profil impossible.", "error");
  }
}

async function onProfileRename() {
  const select = document.getElementById("profileSelect");
  const currentOpt = select.selectedOptions[0];
  if (!currentOpt) return;
  const newName = prompt("Nouveau nom du profil :", currentOpt.textContent);
  if (!newName || !newName.trim() || newName.trim() === currentOpt.textContent) return;
  try {
    const result = await window.pywebview.api.profiles_rename(currentOpt.value, newName.trim());
    if (result && result.ok) {
      state.profiles = result.profiles || [];
      renderProfiles();
      appendLog(`Profil renommé en « ${newName.trim()} ».`, "success");
    } else {
      appendLog(`[Erreur] Renommage du profil : ${(result && result.error) || "échec inconnu"}.`, "error");
    }
  } catch (err) {
    appendLog("[Erreur] Renommage du profil impossible.", "error");
  }
}

async function onProfileDelete() {
  const select = document.getElementById("profileSelect");
  const currentOpt = select.selectedOptions[0];
  if (!currentOpt) return;
  if (state.profiles.length <= 1) {
    appendLog("Impossible de supprimer le dernier profil restant.", "error");
    return;
  }
  if (!confirm(`Supprimer le profil « ${currentOpt.textContent} » ? Ses commandes seront perdues (pas de corbeille). Continuer ?`)) {
    return;
  }
  try {
    const result = await window.pywebview.api.profiles_delete(currentOpt.value);
    if (result && result.ok) {
      state.profiles = result.profiles || [];
      state.activeProfile = result.active;
      if (result.commands) state.commands = result.commands;
      renderProfiles();
      renderCommands();
      appendLog("Profil supprimé.", "info");
    } else {
      appendLog(`[Erreur] Suppression du profil : ${(result && result.error) || "échec inconnu"}.`, "error");
    }
  } catch (err) {
    appendLog("[Erreur] Suppression du profil impossible.", "error");
  }
}

/* ------------------------------------------------------ Config export/import */

async function onExportConfig() {
  const btn = document.getElementById("exportConfigBtn");
  const allProfiles = document.getElementById("exportAllProfilesCheck").checked;
  btn.disabled = true;
  try {
    const result = await window.pywebview.api.export_config(allProfiles);
    if (result && result.ok) {
      appendLog(`Configuration exportée vers ${result.path}.`, "success");
    } else if (result && result.error) {
      appendLog(`[Erreur] Export de la configuration : ${result.error}`, "error");
    }
    // Si result.ok est false SANS erreur, c'est juste que l'utilisateur a
    // annulé la boîte de dialogue : rien à signaler dans ce cas.
  } catch (e) {
    appendLog("[Erreur] Export de la configuration impossible.", "error");
  } finally {
    btn.disabled = false;
  }
}

async function onImportConfig() {
  if (!confirm(
    "Importer une configuration va REMPLACER tes commandes vocales et réglages " +
    "actuels (IA, audio, overlay) par ceux de l'archive choisie.\n\n" +
    "Continuer ?"
  )) {
    return;
  }
  const btn = document.getElementById("importConfigBtn");
  btn.disabled = true;
  try {
    const result = await window.pywebview.api.import_config();
    if (result && result.ok) {
      appendLog("Configuration importée avec succès. Redémarre NOVAVOX pour l'appliquer.", "success");
      if (confirm("Configuration importée. Fermer NOVAVOX maintenant ? (relance-le ensuite pour appliquer la nouvelle configuration)")) {
        window.pywebview.api.close_application();
      }
    } else if (result && result.error) {
      appendLog(`[Erreur] Import de la configuration : ${result.error}`, "error");
    }
  } catch (e) {
    appendLog("[Erreur] Import de la configuration impossible.", "error");
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ Microphone */

async function loadMicDevices() {
  const select = document.getElementById("micSelect");
  const previousValue = select.value;
  select.innerHTML = '<option value="">Chargement des périphériques...</option>';
  try {
    const data = await window.pywebview.api.get_audio_devices();
    select.innerHTML = "";

    const optDefault = document.createElement("option");
    optDefault.value = "";
    optDefault.textContent = "Périphérique par défaut du système";
    select.appendChild(optDefault);

    (data.devices || []).forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d.name;
      opt.textContent = d.isSystemDefault ? `${d.name} (défaut système)` : d.name;
      select.appendChild(opt);
    });

    const wanted = data.selected || previousValue || "";
    // Ne sélectionne que si l'option existe encore dans la liste (le
    // périphérique a pu être débranché entre-temps).
    const exists = [...select.options].some((o) => o.value === wanted);
    select.value = exists ? wanted : "";

    const gainPercent = Math.round((data.gain ?? 1.0) * 100);
    const gainRange = document.getElementById("micGainRange");
    gainRange.value = gainPercent;
    document.getElementById("micGainValue").textContent = `${gainPercent} %`;

    micGateMax = data.gateMax || micGateMax;
    const gateRange = document.getElementById("micGateRange");
    gateRange.max = micGateMax;
    gateRange.value = data.gate ?? 0;
    updateGateMarker(data.gate ?? 0);

    const aecToggle = document.getElementById("aecToggle");
    const aecLabel = aecToggle.closest(".aec-toggle-label");
    aecToggle.checked = !!data.aecEnabled;
    const aecAvailable = data.aecAvailable !== false;
    aecToggle.disabled = !aecAvailable;
    aecLabel.classList.toggle("aec-unavailable", !aecAvailable);
    aecLabel.title = aecAvailable
      ? ""
      : "NumPy n'a pas pu être chargé — l'annulation d'écho est indisponible.";
  } catch (e) {
    select.innerHTML = '<option value="">Erreur de chargement</option>';
  }
}

async function onMicChange(e) {
  const value = e.target.value;
  await window.pywebview.api.set_input_device(value);
  appendLog(
    value ? `Micro sélectionné : ${value}` : "Micro : retour au périphérique par défaut du système.",
    "info"
  );
  // Redémarre la surveillance légère pour que le mètre de niveau reflète
  // immédiatement le nouveau périphérique (sans effet si l'écoute
  // complète est active : elle gère déjà le niveau elle-même).
  if (!state.listening) {
    await window.pywebview.api.stop_mic_monitor();
    await window.pywebview.api.start_mic_monitor();
  }
}

async function onAecToggleChange(e) {
  const enabled = e.target.checked;
  const result = await window.pywebview.api.set_aec_enabled(enabled);
  if (result && result.ok) {
    appendLog(
      `Annulation d'écho ${result.enabled ? "activée" : "désactivée"}.`,
      "info"
    );
  }
}

/* ------------------------------------------------------- Sortie voix */

async function loadOutputDevices() {
  const select = document.getElementById("outputSelect");
  if (!select) return;
  const previousValue = select.value;
  select.innerHTML = '<option value="">Chargement des périphériques...</option>';
  try {
    const data = await window.pywebview.api.get_output_devices();
    select.innerHTML = "";

    const optDefault = document.createElement("option");
    optDefault.value = "";
    optDefault.textContent = "Périphérique par défaut du système";
    select.appendChild(optDefault);

    (data.devices || []).forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d.name;
      opt.textContent = d.isSystemDefault ? `${d.name} (défaut système)` : d.name;
      select.appendChild(opt);
    });

    const wanted = data.selected || previousValue || "";
    const exists = [...select.options].some((o) => o.value === wanted);
    select.value = exists ? wanted : "";

    const volumePercent = Math.round((data.volume ?? 0.6) * 100);
    const volumeRange = document.getElementById("ttsVolumeRange");
    volumeRange.value = volumePercent;
    document.getElementById("ttsVolumeValue").textContent = `${volumePercent} %`;
  } catch (e) {
    select.innerHTML = '<option value="">Erreur de chargement</option>';
  }
}

async function onOutputChange(e) {
  const value = e.target.value;
  await window.pywebview.api.set_output_device(value);
  appendLog(
    value ? `Sortie voix sélectionnée : ${value}` : "Sortie voix : retour au périphérique par défaut du système.",
    "info"
  );
}

// Même logique que onMicGainInput/onMicGainChange : affichage en direct
// pendant le glisser, appel Python seulement au relâchement.
function onTtsVolumeInput(e) {
  document.getElementById("ttsVolumeValue").textContent = `${e.target.value} %`;
}

async function onTtsVolumeChange(e) {
  const percent = Number(e.target.value);
  await window.pywebview.api.set_tts_volume(percent / 100);
}

// Met à jour l'affichage en direct pendant qu'on fait glisser le curseur
// (sans appeler Python à chaque pixel, seulement au relâchement — voir
// onMicGainChange).
function onMicGainInput(e) {
  document.getElementById("micGainValue").textContent = `${e.target.value} %`;
}

async function onMicGainChange(e) {
  const percent = Number(e.target.value);
  const gain = percent / 100;
  await window.pywebview.api.set_mic_gain(gain);
  appendLog(`Volume du micro (interne) réglé à ${percent} %.`, "info");
}

// Appelé par Python à intervalles réguliers pendant l'écoute, avec le
// niveau RMS brut du bloc audio le plus récent.
function micLevelUpdate(rms) {
  const pct = Math.max(0, Math.min(100, (rms / micGateMax) * 100));
  const fill = document.getElementById("micLevelFill");
  if (fill) fill.style.width = `${pct}%`;
}

function updateGateMarker(value) {
  const pct = Math.max(0, Math.min(100, (value / micGateMax) * 100));
  const marker = document.getElementById("micGateMarker");
  if (marker) marker.style.left = `${pct}%`;
  const label = document.getElementById("micGateValue");
  if (label) label.textContent = value > 0 ? `${value}` : "Désactivé";
}

function onMicGateInput(e) {
  updateGateMarker(Number(e.target.value));
}

async function onMicGateChange(e) {
  const value = Number(e.target.value);
  await window.pywebview.api.set_mic_gate(value);
  appendLog(
    value > 0
      ? `Seuil de sensibilité micro réglé à ${value} (le son plus faible sera ignoré).`
      : "Seuil de sensibilité micro désactivé.",
    "info"
  );
}

/* ---------------------------------------------- Mode d'activation vocale */

const LISTEN_MODE_LABELS = {
  always: "Toujours activer",
  toggle_key: "Touche bascule",
  push_to_talk: "Push-to-talk",
};

function listenHotkeyDisplayLabel() {
  if (!state.listenHotkey) return "Non définie";
  const value = String(state.listenHotkey);
  if (value.startsWith("joy:")) {
    try {
      const info = JSON.parse(value.slice(4));
      return `🕹 Bouton ${info.button}`;
    } catch (e) {
      return "🕹 Bouton joystick";
    }
  }
  return value
    .split("+")
    .map((k) => keyDisplayLabel(k.trim().toLowerCase()))
    .join(" + ");
}

function renderListenModeUI() {
  document.querySelectorAll('input[name="listenMode"]').forEach((radio) => {
    radio.checked = radio.value === state.listenMode;
  });

  const row = document.getElementById("listenHotkeyRow");
  const needsHotkey = state.listenMode === "toggle_key" || state.listenMode === "push_to_talk";
  row.classList.toggle("disabled", !needsHotkey);

  document.getElementById("listenHotkeyValue").textContent = listenHotkeyDisplayLabel();

  const hint = document.getElementById("listenHotkeyUnavailableHint");
  hint.style.display = state.listenHotkeyAvailable ? "none" : "block";
  updateMicGateBadge();
}

async function onListenModeChange(e) {
  const mode = e.target.value;
  const res = await window.pywebview.api.set_listen_mode(mode);
  state.listenMode = res.listenMode || mode;
  state.listenHotkey = res.listenHotkey || null;
  state.listenHotkeyAvailable = res.listenHotkeyAvailable !== false;
  renderListenModeUI();

  if ((state.listenMode === "toggle_key" || state.listenMode === "push_to_talk") && !state.listenHotkey) {
    appendLog("Choisis une touche pour ce mode d'activation ci-dessous.", "info");
  } else {
    appendLog(`Mode d'activation vocale : ${LISTEN_MODE_LABELS[state.listenMode] || state.listenMode}.`, "info");
  }
}

function onOpenListenHotkey() {
  openKeyboard(async (combo) => {
    const res = await window.pywebview.api.set_listen_hotkey(combo);
    state.listenHotkey = res.listenHotkey || null;
    state.listenHotkeyAvailable = res.listenHotkeyAvailable !== false;
    renderListenModeUI();
    appendLog(`Touche d'activation vocale réglée sur « ${listenHotkeyDisplayLabel()} ».`, "info");
  }, state.listenHotkey || "", { showHold: false });
}

let joystickCaptureActive = false;

async function onCaptureJoystick() {
  if (joystickCaptureActive) {
    joystickCaptureActive = false;
    await window.pywebview.api.cancel_joystick_capture();
    setJoystickCaptureButtonState(false);
    return;
  }
  joystickCaptureActive = true;
  setJoystickCaptureButtonState(true);
  appendLog("Appuie maintenant sur le bouton du joystick à assigner (15 secondes, Échap pour annuler)...", "info");
  await window.pywebview.api.capture_joystick_button();
}

function setJoystickCaptureButtonState(active) {
  const btn = document.getElementById("captureJoystickBtn");
  if (!btn) return;
  btn.textContent = active ? "⏳ Appuie sur le bouton... (annuler)" : "🕹 Bouton joystick";
  btn.classList.toggle("active", active);
}

// Appelé par Python (voir capture_joystick_button / _joystick_capture_thread)
// une fois un bouton détecté, ou en cas d'échec/timeout/annulation.
async function joystickCaptureResult(result) {
  joystickCaptureActive = false;
  setJoystickCaptureButtonState(false);

  if (!result || !result.ok) {
    const reason = result ? result.reason : "error";
    const messages = {
      cancelled: "Détection annulée.",
      timeout: "Aucun bouton détecté (15 secondes écoulées). Réessaie.",
      no_pygame: "Le module « pygame » est manquant côté application : le support joystick ne peut pas fonctionner. Réinstalle les dépendances (requirements.txt).",
      no_device: "Aucun joystick/manette détecté. Vérifie que le VirPil est bien branché et reconnu par Windows.",
      error: "Erreur pendant la détection du bouton.",
    };
    appendLog(messages[reason] || messages.error, reason === "cancelled" ? "info" : "error");
    return;
  }

  const combo = "joy:" + JSON.stringify({ name: result.name, guid: result.guid, button: result.button });
  const res = await window.pywebview.api.set_listen_hotkey(combo);
  state.listenHotkey = res.listenHotkey || null;
  state.listenHotkeyAvailable = res.listenHotkeyAvailable !== false;
  renderListenModeUI();
  appendLog(`Touche d'activation vocale réglée sur « ${listenHotkeyDisplayLabel()} ».`, "success");
}

async function onClearListenHotkey() {
  const res = await window.pywebview.api.set_listen_hotkey("");
  state.listenHotkey = res.listenHotkey || null;
  state.listenHotkeyAvailable = res.listenHotkeyAvailable !== false;
  renderListenModeUI();
  appendLog("Touche d'activation vocale effacée.", "info");
}

/* ---------------------------------------------- Configuration du modèle vocal */

function openModelSetup(models) {
  const list = document.getElementById("modelChoiceList");
  list.innerHTML = "";
  models.forEach((m) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "model-choice-card";
    card.innerHTML = `
      <div class="model-choice-title">${escapeHtml(m.label)}</div>
      <div class="model-choice-desc">${escapeHtml(m.description)}</div>
    `;
    card.addEventListener("click", () => startModelDownload(m.id, m.label));
    list.appendChild(card);
  });

  document.getElementById("modelSetupChoice").classList.remove("hidden");
  document.getElementById("modelSetupProgress").classList.add("hidden");
  document.getElementById("modelSetupError").classList.add("hidden");
  document.getElementById("modelSetupModal").classList.remove("hidden");
}

function closeModelSetup() {
  document.getElementById("modelSetupModal").classList.add("hidden");
}

async function startModelDownload(modelId, modelLabel) {
  document.getElementById("modelSetupChoice").classList.add("hidden");
  document.getElementById("modelSetupError").classList.add("hidden");
  const progressWrap = document.getElementById("modelSetupProgress");
  const bar = document.getElementById("modelSetupProgressBar");
  const label = document.getElementById("modelSetupProgressLabel");
  progressWrap.classList.remove("hidden");
  bar.style.width = "0%";
  label.textContent = `Préparation du téléchargement de « ${modelLabel} »...`;

  appendLog(`Téléchargement du modèle vocal « ${modelLabel} »...`, "info");
  const res = await window.pywebview.api.download_vosk_model(modelId);
  if (!res.ok) {
    showModelSetupError(res.error || "Impossible de démarrer le téléchargement.");
  }
}

// Appelé par Python pendant le téléchargement/l'extraction
function voskDownloadProgress(percent, message) {
  const bar = document.getElementById("modelSetupProgressBar");
  const label = document.getElementById("modelSetupProgressLabel");
  if (bar) bar.style.width = `${percent}%`;
  if (label) label.textContent = message;
}

// Appelé par Python une fois le téléchargement terminé (succès ou échec)
function voskDownloadDone(success, pathOrError) {
  if (success) {
    document.getElementById("modelPath").value = pathOrError;
    appendLog(`Modèle vocal installé : ${pathOrError}`, "success");
    closeModelSetup();
  } else {
    showModelSetupError(
      `Le téléchargement a échoué (${pathOrError}). Vérifie ta connexion internet, ou choisis ` +
      `« Parcourir... » pour utiliser un modèle déjà téléchargé manuellement.`
    );
  }
}

function showModelSetupError(message) {
  document.getElementById("modelSetupProgress").classList.add("hidden");
  document.getElementById("modelSetupChoice").classList.remove("hidden");
  const errEl = document.getElementById("modelSetupError");
  errEl.textContent = message;
  errEl.classList.remove("hidden");
  appendLog(`[Erreur] ${message}`, "error");
}

/* ------------------------------------------------------------ Écoute */

async function toggleListening() {
  const btn = document.getElementById("engageBtn");

  if (!state.listening) {
    const modelPath = document.getElementById("modelPath").value.trim();
    if (!modelPath) {
      appendLog("[Erreur] Sélectionnez d'abord un dossier de modèle Vosk.", "error");
      return;
    }
    setStatus("loading", "Chargement...", "Initialisation du modèle");
    btn.disabled = true;
    const res = await window.pywebview.api.start_listening(modelPath);
    btn.disabled = false;

    if (!res.ok) {
      setStatus("error", "Erreur", res.error);
      appendLog(`[Erreur] ${res.error}`, "error");
      return;
    }
    state.listening = true;
    state.micGateOpen = true;
    btn.textContent = "Couper l'écoute";
    btn.classList.remove("btn-engage");
    btn.classList.add("btn-danger");
    updateMicGateBadge();
  } else {
    await window.pywebview.api.stop_listening();
    state.listening = false;
    btn.textContent = "Engager l'écoute";
    btn.classList.remove("btn-danger");
    btn.classList.add("btn-engage");
    setStatus("idle", "Arrêté", "Système en veille");
  }
}

/* ---------------------------------------- Fonctions appelées par Python */

function setStatus(status, label, sub) {
  const core = document.getElementById("statusCore");
  core.className = "status-core " + status;
  document.getElementById("statusLabel").textContent = label;
  if (sub !== undefined && sub !== null) {
    document.getElementById("statusSub").textContent = sub;
  }
  // Si le flux s'arrête de façon inattendue côté Python, resynchronise le bouton
  if (status === "idle" || status === "error") {
    state.listening = false;
    const btn = document.getElementById("engageBtn");
    btn.textContent = "Engager l'écoute";
    btn.classList.remove("btn-danger");
    btn.classList.add("btn-engage");
    btn.disabled = false;
  } else if (status === "listening") {
    state.listening = true;
    state.micGateOpen = true;
  }
  updateAiWakeDot();
  updateMicGateBadge();
}

// Appelé par Python à chaque changement d'état du micro (touche
// push-to-talk relâchée/maintenue, ou bascule ON/OFF) — voir _set_mic_gate.
function micGateUpdate(open) {
  state.micGateOpen = !!open;
  updateMicGateBadge();
}

function updateMicGateBadge() {
  const badge = document.getElementById("micGateBadge");
  if (!badge) return;
  const relevant = state.listening && (state.listenMode === "toggle_key" || state.listenMode === "push_to_talk");
  if (!relevant) {
    badge.classList.add("hidden");
    return;
  }
  badge.classList.remove("hidden");
  if (state.micGateOpen) {
    badge.className = "mic-gate-badge open";
    badge.textContent = "🎙 Micro actif";
  } else {
    badge.className = "mic-gate-badge muted";
    badge.textContent = "🔇 Micro coupé";
  }
}

function appendLog(msg, kind) {
  const consoleEl = document.getElementById("logConsole");
  const line = document.createElement("div");
  line.className = "log-line log-" + (kind || "info");
  const time = new Date().toLocaleTimeString("fr-FR", { hour12: false });
  line.innerHTML = `<span class="log-time">${time}</span><span class="log-msg">${escapeHtml(msg)}</span>`;
  consoleEl.appendChild(line);
  consoleEl.scrollTop = consoleEl.scrollHeight;

  // Toute erreur affichée dans le panneau (qu'elle vienne de Python via
  // _log, ou d'une validation faite ici côté JS, ex. "dossier introuvable")
  // est aussi envoyée à Python pour être écrite dans erreurs.log. C'est le
  // seul endroit où une erreur est réellement montrée à l'utilisateur,
  // donc le seul endroit fiable pour centraliser le journal fichier.
  if (kind === "error" && window.pywebview && window.pywebview.api && window.pywebview.api.log_error) {
    window.pywebview.api.log_error(msg).catch(() => {});
  }
}

/* ------------------------------------------------- Clavier virtuel */

const KB_MODIFIERS = ["ctrl", "alt", "shift", "altright"];

// Disposition visuelle façon QWERTY. "value" = nom de touche pydirectinput.
// Clavier principal : 6 rangées reproduisant un clavier physique complet.
const KB_LAYOUT = [
  [
    { label: "Esc", value: "esc" },
    { label: "F1", value: "f1" }, { label: "F2", value: "f2" },
    { label: "F3", value: "f3" }, { label: "F4", value: "f4" },
    { label: "F5", value: "f5" }, { label: "F6", value: "f6" },
    { label: "F7", value: "f7" }, { label: "F8", value: "f8" },
    { label: "F9", value: "f9" }, { label: "F10", value: "f10" },
    { label: "F11", value: "f11" }, { label: "F12", value: "f12" },
  ],
  [
    { label: "`", value: "`" },
    { label: "1", value: "1" }, { label: "2", value: "2" }, { label: "3", value: "3" },
    { label: "4", value: "4" }, { label: "5", value: "5" }, { label: "6", value: "6" },
    { label: "7", value: "7" }, { label: "8", value: "8" }, { label: "9", value: "9" },
    { label: "0", value: "0" },
    { label: "-", value: "-" }, { label: "=", value: "=" },
    { label: "←", value: "backspace", wide: true },
  ],
  [
    { label: "Tab", value: "tab", wide: true },
    { label: "Q", value: "q" }, { label: "W", value: "w" }, { label: "E", value: "e" },
    { label: "R", value: "r" }, { label: "T", value: "t" }, { label: "Y", value: "y" },
    { label: "U", value: "u" }, { label: "I", value: "i" }, { label: "O", value: "o" },
    { label: "P", value: "p" },
    { label: "[", value: "[" }, { label: "]", value: "]" }, { label: "\\", value: "\\" },
  ],
  [
    { label: "Verr.Maj", value: "capslock", wide: true },
    { label: "A", value: "a" }, { label: "S", value: "s" }, { label: "D", value: "d" },
    { label: "F", value: "f" }, { label: "G", value: "g" }, { label: "H", value: "h" },
    { label: "J", value: "j" }, { label: "K", value: "k" }, { label: "L", value: "l" },
    { label: ";", value: ";" }, { label: "'", value: "'" },
    { label: "Entrée", value: "enter", wide: true },
  ],
  [
    { label: "Shift", value: "shift", modifier: true, wide: true },
    { label: "<>\\", value: "iso102" },
    { label: "Z", value: "z" }, { label: "X", value: "x" }, { label: "C", value: "c" },
    { label: "V", value: "v" }, { label: "B", value: "b" }, { label: "N", value: "n" },
    { label: "M", value: "m" },
    { label: ",", value: "," }, { label: ".", value: "." }, { label: "/", value: "/" },
    { label: "Shift", value: "shift", modifier: true, wide: true },
  ],
  [
    { label: "Ctrl", value: "ctrl", modifier: true },
    { label: "⊞", value: "winleft" },
    { label: "Alt", value: "alt", modifier: true },
    { label: "Espace", value: "space", spacebar: true },
    { label: "AltGr", value: "altright", modifier: true },
    { label: "⊞", value: "winleft" },
    { label: "Menu", value: "apps" },
    { label: "Ctrl", value: "ctrl", modifier: true },
  ],
];

// Bloc navigation, aligné rangée par rangée sur le clavier principal
// (impr.écran/défil/pause, ins/origine/pgUp, suppr/fin/pgDn, puis flèches).
const KB_NAV_LAYOUT = [
  [
    { label: "Impr", value: "printscreen" },
    { label: "Défil", value: "scrolllock" },
    { label: "Pause", value: "pause" },
  ],
  [
    { label: "Ins", value: "insert" },
    { label: "Orig.", value: "home" },
    { label: "PgUp", value: "pageup" },
  ],
  [
    { label: "Suppr", value: "delete" },
    { label: "Fin", value: "end" },
    { label: "PgDn", value: "pagedown" },
  ],
  [{ ghost: true }],
  [{ ghost: true }, { label: "▲", value: "up" }, { ghost: true }],
  [
    { label: "◄", value: "left" },
    { label: "▼", value: "down" },
    { label: "►", value: "right" },
  ],
];

// Pavé numérique, aligné sur les rangées 2 à 6 du clavier principal
// (une ligne fantôme comble la rangée Esc/F1-F12 pour garder l'alignement).
const KB_NUMPAD_LAYOUT = [
  [{ ghost: true }],
  [
    { label: "Verr.Num", value: "numlock", wide: true },
    { label: "/", value: "divide" },
    { label: "*", value: "multiply" },
  ],
  [
    { label: "7", value: "num7" }, { label: "8", value: "num8" }, { label: "9", value: "num9" },
    { label: "-", value: "subtract" },
  ],
  [
    { label: "4", value: "num4" }, { label: "5", value: "num5" }, { label: "6", value: "num6" },
    { label: "+", value: "add" },
  ],
  [
    { label: "1", value: "num1" }, { label: "2", value: "num2" }, { label: "3", value: "num3" },
  ],
  [
    { label: "0", value: "num0", wide: true },
    { label: ",", value: "decimal" },
    { label: "↵", value: "enter" },
  ],
];

const kbState = { modifiers: new Set(), mainKey: null };

// Boutons souris : traités exactement comme une touche clavier normale
// (valeur "mainKey" combinable avec Ctrl/Alt/Maj), mais exécutés côté
// Python comme un clic souris plutôt qu'une frappe clavier (voir
// _press_keys dans app.py).
const KB_MOUSE_LAYOUT = [
  [
    { label: "🖱️ Gauche", value: "mouseleft", wider: true },
    { label: "🖱️ Droit", value: "mouseright", wider: true },
    { label: "🖱️ Molette", value: "mousemiddle", wider: true },
  ],
];

// Correspondance event.code (position physique, indépendante de la
// disposition clavier système) -> "value" pydirectinput utilisées dans
// KB_LAYOUT/KB_NAV_LAYOUT/KB_NUMPAD_LAYOUT ci-dessus. On utilise .code et
// non .key car .key donne le CARACTÈRE produit (donc dépend de la
// disposition système, ex. "q" devient "a" en AZERTY), alors que .code
// donne la position physique de la touche — exactement ce qu'on veut
// puisque ces "value" désignent déjà des positions, pas des caractères.
const KB_CODE_TO_VALUE = {
  Escape: "esc",
  F1: "f1", F2: "f2", F3: "f3", F4: "f4", F5: "f5", F6: "f6",
  F7: "f7", F8: "f8", F9: "f9", F10: "f10", F11: "f11", F12: "f12",
  Backquote: "`",
  Digit1: "1", Digit2: "2", Digit3: "3", Digit4: "4", Digit5: "5",
  Digit6: "6", Digit7: "7", Digit8: "8", Digit9: "9", Digit0: "0",
  Minus: "-", Equal: "=", Backspace: "backspace",
  Tab: "tab",
  KeyQ: "q", KeyW: "w", KeyE: "e", KeyR: "r", KeyT: "t", KeyY: "y",
  KeyU: "u", KeyI: "i", KeyO: "o", KeyP: "p",
  BracketLeft: "[", BracketRight: "]", Backslash: "\\",
  CapsLock: "capslock",
  KeyA: "a", KeyS: "s", KeyD: "d", KeyF: "f", KeyG: "g", KeyH: "h",
  KeyJ: "j", KeyK: "k", KeyL: "l",
  Semicolon: ";", Quote: "'", Enter: "enter",
  ShiftLeft: "shift", ShiftRight: "shift",
  KeyZ: "z", KeyX: "x", KeyC: "c", KeyV: "v", KeyB: "b", KeyN: "n", KeyM: "m",
  Comma: ",", Period: ".", Slash: "/",
  IntlBackslash: "iso102",
  ControlLeft: "ctrl", ControlRight: "ctrl",
  MetaLeft: "winleft", MetaRight: "winleft",
  AltLeft: "alt", AltRight: "altright",
  Space: "space", ContextMenu: "apps",
  PrintScreen: "printscreen", ScrollLock: "scrolllock", Pause: "pause",
  Insert: "insert", Home: "home", PageUp: "pageup",
  Delete: "delete", End: "end", PageDown: "pagedown",
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  NumLock: "numlock", NumpadDivide: "divide", NumpadMultiply: "multiply",
  Numpad7: "num7", Numpad8: "num8", Numpad9: "num9", NumpadSubtract: "subtract",
  Numpad4: "num4", Numpad5: "num5", Numpad6: "num6", NumpadAdd: "add",
  Numpad1: "num1", Numpad2: "num2", Numpad3: "num3",
  Numpad0: "num0", NumpadDecimal: "decimal", NumpadEnter: "enter",
};

let kbCaptureActive = false;

// ------------------------------------------------- Dispositions clavier
//
// Les "value" (noms de touches pydirectinput) désignent une POSITION
// physique sur le clavier — pas un caractère. C'est pour ça qu'on ne
// touche jamais aux "value" ci-dessus : seul le LIBELLÉ affiché sur la
// touche change selon la disposition choisie, pour correspondre à ce que
// l'utilisateur voit réellement sur son propre clavier. La touche
// physique envoyée au jeu reste donc correcte quelle que soit la
// disposition affichée.
//
// AZERTY France et Belgique partagent la même disposition des lettres
// (A/Q et Z/W intervertis, M remonté d'une rangée, virgule/point-virgule
// permutés) ; seules quelques touches de ponctuation diffèrent un peu
// d'un clavier à l'autre selon le modèle exact.
const AZERTY_LETTERS = {
  q: "A", w: "Z", a: "Q", z: "W",
  ";": "M", m: ",", ",": ";", ".": ":", "/": "!",
  "'": "Ù", "`": "²", "[": "^", "]": "$",
  "-": ")",
  // Rangée des chiffres : ce qui sort réellement sans Maj sur un clavier
  // AZERTY (les chiffres eux-mêmes ne s'obtiennent qu'avec Maj).
  1: "&", 2: "é", 3: "\"", 4: "'", 5: "(", 7: "è", 9: "ç", 0: "à",
};

const KB_LAYOUTS = {
  qwerty: {},
  // Vérifiées sur clavier de référence : lettres identiques FR/BE
  // (A/Q, Z/W, M, virgule/point-virgule...). Seules quelques touches de
  // ponctuation et deux touches de la rangée des chiffres (6 et 8)
  // diffèrent réellement entre les deux pays.
  azerty_fr: { ...AZERTY_LETTERS, "\\": "*", 6: "-", 8: "_", iso102: "< >" },
  azerty_be: { ...AZERTY_LETTERS, "\\": "µ", "=": "-", "/": "=", 6: "§", 8: "!", iso102: "< > \\" },
};

const KB_LAYOUT_NAMES = {
  qwerty: "QWERTY",
  azerty_fr: "AZERTY France",
  azerty_be: "AZERTY Belgique",
};

let kbLayoutChoice = localStorage.getItem("kbLayout") || "azerty_fr";
if (!KB_LAYOUTS[kbLayoutChoice]) kbLayoutChoice = "azerty_fr";

// Libellés QWERTY de base pour chaque "value", tous claviers confondus
// (utilisé comme repli et pour l'affichage des combinaisons déjà
// enregistrées, ex. dans la liste des commandes).
const BASE_KEY_LABELS = {};
[KB_LAYOUT, KB_NAV_LAYOUT, KB_NUMPAD_LAYOUT, KB_MOUSE_LAYOUT].forEach((layout) => {
  layout.forEach((row) => {
    row.forEach((key) => {
      if (!key.ghost) BASE_KEY_LABELS[key.value] = key.label;
    });
  });
});

function keyDisplayLabel(value) {
  const overrides = KB_LAYOUTS[kbLayoutChoice] || {};
  if (Object.prototype.hasOwnProperty.call(overrides, value)) return overrides[value];
  return BASE_KEY_LABELS[value] || String(value).toUpperCase();
}

function setKeyboardLayout(layoutKey) {
  if (!KB_LAYOUTS[layoutKey]) return;
  kbLayoutChoice = layoutKey;
  localStorage.setItem("kbLayout", layoutKey);

  document.querySelectorAll(".kb-layout-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.layout === layoutKey);
  });

  document.querySelectorAll("#virtualKeyboard .kb-key[data-value]").forEach((btn) => {
    btn.textContent = keyDisplayLabel(btn.dataset.value);
  });

  refreshKeyboardHighlight();
  renderCommands();
  renderListenModeUI();
  if (window.pywebview && window.pywebview.api && window.pywebview.api.set_kb_layout) {
    window.pywebview.api.set_kb_layout(layoutKey);
  }
}

function renderKeyboardRows(layout, targetEl) {
  layout.forEach((row) => {
    const rowEl = document.createElement("div");
    rowEl.className = "kb-row";
    row.forEach((key) => {
      const btn = document.createElement("button");
      btn.type = "button";

      if (key.ghost) {
        btn.className = "kb-key ghost";
        btn.disabled = true;
        btn.tabIndex = -1;
        rowEl.appendChild(btn);
        return;
      }

      btn.className = "kb-key" + (key.modifier ? " modifier" : " main");
      if (key.wide) btn.classList.add("wide");
      if (key.wider) btn.classList.add("wider");
      if (key.spacebar) btn.classList.add("spacebar");
      btn.textContent = key.label;
      btn.dataset.value = key.value;
      btn.dataset.modifier = key.modifier ? "1" : "0";
      btn.addEventListener("click", onKeyClick);
      rowEl.appendChild(btn);
    });
    targetEl.appendChild(rowEl);
  });
}

function buildVirtualKeyboard() {
  const mainEl = document.getElementById("kbMain");
  const navEl = document.getElementById("kbNav");
  const numpadEl = document.getElementById("kbNumpad");
  const mouseEl = document.getElementById("kbMouse");
  mainEl.innerHTML = "";
  navEl.innerHTML = "";
  numpadEl.innerHTML = "";
  mouseEl.innerHTML = "";

  renderKeyboardRows(KB_LAYOUT, mainEl);
  renderKeyboardRows(KB_NAV_LAYOUT, navEl);
  renderKeyboardRows(KB_NUMPAD_LAYOUT, numpadEl);
  renderKeyboardRows(KB_MOUSE_LAYOUT, mouseEl);

  document.querySelectorAll(".kb-layout-btn").forEach((btn) => {
    btn.addEventListener("click", () => setKeyboardLayout(btn.dataset.layout));
  });
  setKeyboardLayout(kbLayoutChoice);
}

function onKeyClick(e) {
  const btn = e.currentTarget;
  const value = btn.dataset.value;
  const isModifier = btn.dataset.modifier === "1";

  if (isModifier) {
    if (kbState.modifiers.has(value)) {
      kbState.modifiers.delete(value);
    } else {
      kbState.modifiers.add(value);
    }
  } else {
    kbState.mainKey = kbState.mainKey === value ? null : value;
  }

  refreshKeyboardHighlight();
}

function refreshKeyboardHighlight() {
  document.querySelectorAll("#virtualKeyboard .kb-key").forEach((btn) => {
    const value = btn.dataset.value;
    const isModifier = btn.dataset.modifier === "1";
    const active = isModifier ? kbState.modifiers.has(value) : kbState.mainKey === value;
    btn.classList.toggle("active", active);
  });

  const parts = [...KB_MODIFIERS.filter((m) => kbState.modifiers.has(m))];
  if (kbState.mainKey) parts.push(kbState.mainKey);
  const preview = document.getElementById("kbSelectedValue");
  preview.textContent = parts.length ? parts.map(keyDisplayLabel).join(" + ") : "—";

  document.getElementById("kbConfirmBtn").disabled = parts.length === 0;
}

function parseKeysIntoState(value) {
  kbState.modifiers = new Set();
  kbState.mainKey = null;
  (value || "")
    .split("+")
    .map((k) => k.trim().toLowerCase())
    .filter(Boolean)
    .forEach((k) => {
      if (KB_MODIFIERS.includes(k)) {
        kbState.modifiers.add(k);
      } else {
        kbState.mainKey = k;
      }
    });
}

let kbOnConfirm = null;

function openKeyboard(onConfirm, seedValue, options) {
  options = options || {};
  kbOnConfirm = typeof onConfirm === "function" ? onConfirm : null;
  if (typeof seedValue !== "string") {
    seedValue = kbOnConfirm ? "" : document.getElementById("keysInput").value;
  }
  parseKeysIntoState(seedValue);
  refreshKeyboardHighlight();
  stopKeyCapture();

  const showExtras = options.showHold !== false;

  const holdRow = document.getElementById("kbHoldRow");
  const holdToggle = document.getElementById("kbHoldToggle");
  if (holdRow) holdRow.style.display = showExtras ? "" : "none";
  if (holdToggle) holdToggle.checked = !!options.holdSeed;

  const repeatRow = document.getElementById("kbRepeatRow");
  const repeatToggle = document.getElementById("kbRepeatToggle");
  const repeatFields = document.getElementById("kbRepeatFields");
  const repeatCountInput = document.getElementById("kbRepeatCount");
  const repeatDelayInput = document.getElementById("kbRepeatDelay");
  const repeatSeedEnabled = !!options.repeatSeed;
  if (repeatRow) repeatRow.style.display = showExtras ? "" : "none";
  if (repeatToggle) repeatToggle.checked = repeatSeedEnabled;
  if (repeatFields) repeatFields.style.display = repeatSeedEnabled ? "flex" : "none";
  if (repeatCountInput) repeatCountInput.value = options.repeatCountSeed || 3;
  if (repeatDelayInput) repeatDelayInput.value = options.repeatDelaySeed ?? 0.1;

  document.getElementById("keyboardModal").classList.remove("hidden");
}

function onOpenProfileCycleHotkey() {
  openKeyboard(async (combo) => {
    const res = await window.pywebview.api.set_profile_cycle_hotkey(combo);
    state.profileCycleHotkey = res.hotkey || null;
    renderProfileCycleHotkeyUI();
  }, state.profileCycleHotkey || "", { showHold: false });
}

function closeKeyboard() {
  stopKeyCapture();
  if (mouseCaptureActive) stopMouseCapture(true);
  document.getElementById("keyboardModal").classList.add("hidden");
  kbOnConfirm = null;
}

function toggleKeyCapture() {
  if (kbCaptureActive) {
    stopKeyCapture();
  } else {
    startKeyCapture();
  }
}

function startKeyCapture() {
  kbCaptureActive = true;
  const btn = document.getElementById("kbCaptureBtn");
  if (btn) {
    btn.textContent = "⏳ Appuie sur une touche...";
    btn.classList.add("active");
  }
  const hint = document.getElementById("kbCaptureHint");
  if (hint) hint.style.display = "block";
  document.addEventListener("keydown", onKeyCapture, true);
}

function stopKeyCapture() {
  kbCaptureActive = false;
  const btn = document.getElementById("kbCaptureBtn");
  if (btn) {
    btn.textContent = "🎙 Détecter (appuyer sur la touche)";
    btn.classList.remove("active");
  }
  const hint = document.getElementById("kbCaptureHint");
  if (hint) hint.style.display = "none";
  document.removeEventListener("keydown", onKeyCapture, true);
}

function onKeyCapture(e) {
  e.preventDefault();
  e.stopPropagation();

  if (e.code === "Escape") {
    stopKeyCapture();
    return;
  }

  const value = KB_CODE_TO_VALUE[e.code];
  if (!value) return; // touche non prise en charge, on ignore et on continue d'écouter

  if (KB_MODIFIERS.includes(value)) {
    // Modificateur seul : on le mémorise et on continue d'attendre soit
    // une touche principale, soit une validation manuelle (Ctrl seul
    // reste un raccourci valide, voir confirmKeyboard).
    kbState.modifiers.add(value);
    refreshKeyboardHighlight();
    return;
  }

  // Touche principale détectée : on prend aussi en compte les
  // modificateurs réellement maintenus au moment de l'appui (AltGr se
  // détecte via getModifierState("AltGraph"), pas via e.altKey qui ne
  // couvre que l'Alt de gauche sur la plupart des navigateurs).
  kbState.modifiers = new Set(
    KB_MODIFIERS.filter((m) => {
      if (m === "ctrl") return e.ctrlKey;
      if (m === "alt") return e.altKey;
      if (m === "shift") return e.shiftKey;
      if (m === "altright") return e.getModifierState && e.getModifierState("AltGraph");
      return false;
    })
  );
  kbState.mainKey = value;
  refreshKeyboardHighlight();
  stopKeyCapture();
}

function clearKeyboardSelection() {
  kbState.modifiers = new Set();
  kbState.mainKey = null;
  refreshKeyboardHighlight();
}

/* ---------------------------------- Détection physique d'un clic souris */
// Volontairement annulable UNIQUEMENT via Échap (pas un bouton cliquable) :
// tant que la détection est active, un clic gauche n'importe où — y
// compris sur un bouton "Annuler" — serait lui-même capturé comme le clic
// à assigner, avant même que la demande d'annulation n'arrive.

let mouseCaptureActive = false;

async function onCaptureMouseButton() {
  if (mouseCaptureActive) return;
  mouseCaptureActive = true;
  setMouseCaptureButtonState(true);
  document.addEventListener("keydown", onMouseCaptureEscape, true);
  await window.pywebview.api.capture_mouse_button();
}

function onMouseCaptureEscape(e) {
  if (e.key !== "Escape") return;
  e.preventDefault();
  e.stopPropagation();
  stopMouseCapture(true);
}

async function stopMouseCapture(sendCancel) {
  mouseCaptureActive = false;
  setMouseCaptureButtonState(false);
  document.removeEventListener("keydown", onMouseCaptureEscape, true);
  if (sendCancel) {
    await window.pywebview.api.cancel_mouse_capture();
  }
}

function setMouseCaptureButtonState(active) {
  const btn = document.getElementById("kbMouseCaptureBtn");
  const hint = document.getElementById("kbMouseCaptureHint");
  if (btn) {
    btn.textContent = active ? "⏳ Clique avec la souris..." : "🖱️ Détecter (cliquer)";
    btn.classList.toggle("active", active);
  }
  if (hint) hint.style.display = active ? "block" : "none";
}

// Appelé par Python (voir capture_mouse_button / _mouse_capture_thread)
// une fois un clic détecté, ou en cas d'échec/timeout/annulation.
function mouseCaptureResult(result) {
  stopMouseCapture(false);

  if (!result || !result.ok) {
    const reason = result ? result.reason : "error";
    const messages = {
      cancelled: "Détection annulée.",
      timeout: "Aucun clic détecté (15 secondes écoulées). Réessaie.",
      no_mouse_lib: "Le module « mouse » est manquant côté application : la détection physique du clic ne peut pas fonctionner. Utilise plutôt les boutons Gauche/Droit/Molette ci-dessus, ou réinstalle les dépendances (requirements.txt).",
      error: "Erreur pendant la détection du clic.",
    };
    appendLog(messages[reason] || messages.error, reason === "cancelled" ? "info" : "error");
    return;
  }

  kbState.mainKey = result.button;
  refreshKeyboardHighlight();
}

function confirmKeyboard() {
  const parts = [...KB_MODIFIERS.filter((m) => kbState.modifiers.has(m))];
  if (kbState.mainKey) parts.push(kbState.mainKey);
  if (!parts.length) return;
  const combo = parts.join("+");

  const holdToggle = document.getElementById("kbHoldToggle");
  const holdChecked = !!(holdToggle && holdToggle.checked);

  const repeatToggle = document.getElementById("kbRepeatToggle");
  const repeatEnabled = !!(repeatToggle && repeatToggle.checked);
  const repeatCount = repeatEnabled
    ? Math.max(2, parseInt(document.getElementById("kbRepeatCount").value, 10) || 2)
    : 1;
  const repeatDelay = Math.max(0, parseFloat(document.getElementById("kbRepeatDelay").value) || 0);

  if (kbOnConfirm) {
    const cb = kbOnConfirm;
    kbOnConfirm = null;
    stopKeyCapture();
    document.getElementById("keyboardModal").classList.add("hidden");
    cb(combo, holdChecked, repeatCount, repeatDelay);
  } else {
    const keysInput = document.getElementById("keysInput");
    keysInput.value = combo;
    keysInput.dataset.hold = holdChecked ? "1" : "0";
    keysInput.dataset.repeatCount = String(repeatCount);
    keysInput.dataset.repeatDelay = String(repeatDelay);
    closeKeyboard();
  }
}

/* -------------------------------------------- Flash visuel de commande */

function flashCommand(index) {
  const card = document.querySelector(`.command-card[data-index="${index}"]`);
  if (!card) return;
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 650);
}

function updateContextCount() {
  const val = document.getElementById("aiContextInput").value;
  const el = document.getElementById("aiContextCount");
  if (el) el.textContent = `${val.length} caractères`;
}

async function saveAiContext() {
  const val = document.getElementById("aiContextInput").value.trim();
  const saved = await window.pywebview.api.ai_set_custom_context(val);
  document.getElementById("aiContextInput").value = saved;
  updateContextCount();
  appendLog(
    saved ? "Connaissances personnalisées enregistrées pour l'assistant IA." : "Connaissances personnalisées effacées.",
    "info"
  );
}

/* ---------------------------------------------------- Assistant IA */

async function openAiChat() {
  document.getElementById("aiModal").classList.remove("hidden");
  updateAiWakeDot();

  try {
    const data = await window.pywebview.api.ai_get_state();
    document.getElementById("aiChat").innerHTML = "";
    (data.history || []).forEach((m) => appendAiMessage(m.role, m.content));
    document.getElementById("aiVoiceOutputToggle").checked = data.voiceOutput !== false;
    updateAiName(data.name);
    updateUserNameDisplay(data.userName);
  } catch (e) {
    // état IA non disponible, ignore
  }

  refreshAiStatus();
}

async function loadAiSettingsTab() {
  try {
    const data = await window.pywebview.api.ai_get_state();
    document.getElementById("aiConfirmCommandsToggle").checked = !!data.confirmCommands;
    document.getElementById("aiCooldownInput").value = data.triggerCooldown ?? 3.0;
    document.getElementById("aiContextInput").value = data.customContext || "";
    updateContextCount();
    document.getElementById("aiResponseLengthSelect").value = data.responseLength || "normal";
    loadModelOptions(data.availableModels || [], data.model);

    state.piperVoice = data.piperVoice || null;
    document.getElementById("radioEffectToggle").checked = !!data.radioEffect;
    setPiperSpeedValue(data.piperLengthScale ?? 1.0);
    setPiperExpressivenessValue(data.piperNoiseScale ?? 0.667);
    await refreshPiperStatus();
  } catch (e) {
    // état IA non disponible, ignore
  }
}

function loadModelOptions(models, selectedModel) {
  const select = document.getElementById("aiModelSelect");
  select.innerHTML = "";
  models.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.label;
    opt.dataset.description = m.description || "";
    select.appendChild(opt);
  });
  select.value = selectedModel || (models[0] && models[0].id) || "";
  updateModelDesc(select.value);
}

function updateModelDesc(modelId) {
  const select = document.getElementById("aiModelSelect");
  const opt = [...select.options].find((o) => o.value === modelId);
  const desc = opt ? opt.dataset.description : "";
  document.getElementById("aiModelDesc").textContent = modelId ? `${desc} (${modelId})` : desc;
}

async function onUninstallModel() {
  const select = document.getElementById("aiModelSelect");
  const modelId = select.value;
  if (!modelId) return;
  const confirmed = confirm(`Désinstaller le modèle « ${modelId} » ? Il faudra le retélécharger pour le réutiliser.`);
  if (!confirmed) return;

  appendLog(`[Info] Désinstallation de « ${modelId} » en cours...`, "info");
  await window.pywebview.api.ai_uninstall_model(modelId);
}

// Appelé par Python une fois la désinstallation terminée
function aiUninstallDone(success) {
  refreshAiStatus();
}

function updateAiName(name) {
  if (!name) return;
  document.getElementById("aiPanelTitle").textContent = `Assistant IA (${name})`;
  document.getElementById("aiWakeName").textContent = `« ${name} »`;
}

async function renameAi() {
  const current = document.getElementById("aiWakeName").textContent.replace(/[«»\s]/g, "");
  const next = prompt("Quel nom veux-tu donner à l'assistant ?", current);
  if (next && next.trim()) {
    const newName = await window.pywebview.api.ai_set_name(next.trim());
    updateAiName(newName);
    appendLog(`[Info] L'assistant s'appelle maintenant « ${newName} ».`, "info");
  }
}

function updateUserNameDisplay(name) {
  const el = document.getElementById("aiUserNameDisplay");
  if (!el) return;
  el.textContent = name && name.trim() ? name.trim() : "prénom non renseigné";
}

async function editUserName() {
  const current = document.getElementById("aiUserNameDisplay").textContent;
  const seed = current === "prénom non renseigné" ? "" : current;
  const next = prompt("Comment veux-tu que l'assistant t'appelle ?", seed);
  if (next === null) return; // annulé
  const saved = await window.pywebview.api.ai_set_user_name(next.trim());
  updateUserNameDisplay(saved);
  appendLog(
    saved ? `[Info] Prénom enregistré : « ${saved} ». L'assistant s'adressera à toi par ce prénom.` : "[Info] Prénom effacé.",
    "info"
  );
}

/* ------------------------------------------------- Moteur vocal Piper */

async function refreshPiperStatus() {
  const status = await window.pywebview.api.piper_get_status();
  const notInstalled = document.getElementById("piperNotInstalled");
  const tuning = document.getElementById("piperTuning");
  const voiceList = document.getElementById("piperVoiceList");

  if (!status.engineInstalled) {
    notInstalled.classList.remove("hidden");
    tuning.classList.add("hidden");
    voiceList.classList.add("hidden");
    return;
  }
  notInstalled.classList.add("hidden");
  tuning.classList.remove("hidden");
  voiceList.classList.remove("hidden");
  renderPiperVoices(status.voices || []);
  loadOutputDevices();
}

function setPiperSpeedValue(value) {
  const range = document.getElementById("piperSpeedRange");
  const label = document.getElementById("piperSpeedValue");
  range.value = value;
  label.textContent = `${Number(value).toFixed(2)}×`;
}

function setPiperExpressivenessValue(value) {
  const range = document.getElementById("piperExpressivenessRange");
  const label = document.getElementById("piperExpressivenessValue");
  range.value = value;
  label.textContent = Number(value).toFixed(2);
}

async function onRadioEffectToggle(e) {
  await window.pywebview.api.ai_set_radio_effect(e.target.checked);
  appendLog(
    e.target.checked
      ? "🛰️ Effet communication vaisseau activé."
      : "Effet communication vaisseau désactivé.",
    "info"
  );
}

/* ---------------------------------------------------------- Onglet Game.log */

async function loadGameLogSettingsTab() {
  try {
    const data = await window.pywebview.api.ai_get_state();
    document.getElementById("gameLogToggle").checked = !!data.gameLogEnabled;
    document.getElementById("gameLogAnnounceToggle").checked = data.gameLogAnnounce !== false;
    document.getElementById("gameLogPlayerHandleInput").value = data.gameLogPlayerHandle || "";
  } catch (e) {
    // état Game.log non disponible, ignore
  }
}

async function loadGameLogEntriesPanel() {
  try {
    // Réutilise ai_get_state : elle renvoie déjà tous les champs
    // gameLog* nécessaires (phrases, corrections de notifications), pas
    // la peine d'un point d'entrée dédié côté Python pour un simple
    // affichage.
    const data = await window.pywebview.api.ai_get_state();
    renderGameLogPhrases(
      data.gameLogPhrases || {},
      data.gameLogPhraseDefaults || {},
      data.gameLogPhraseMeta || {}
    );
    state.gameLogHudOverrides = data.gameLogHudOverrides || {};
    renderGameLogHudOverrides(state.gameLogHudOverrides);
  } catch (e) {
    // entrées Game.log non disponibles, ignore
  }
}

function openGameLogEntriesModal() {
  document.getElementById("gameLogEntriesModal").classList.remove("hidden");
  loadGameLogEntriesPanel();
}

function closeGameLogEntriesModal() {
  document.getElementById("gameLogEntriesModal").classList.add("hidden");
}

async function onGameLogToggle(e) {
  const res = await window.pywebview.api.set_game_log_enabled(e.target.checked);
  appendLog(
    res.enabled
      ? "🛰 Surveillance du Game.log activée."
      : "Surveillance du Game.log désactivée.",
    "info"
  );
}

async function onGameLogAnnounceToggle(e) {
  await window.pywebview.api.set_game_log_announce(e.target.checked);
}

async function onSaveGameLogPlayerHandle() {
  const input = document.getElementById("gameLogPlayerHandleInput");
  const value = input.value.trim();
  const saved = await window.pywebview.api.set_game_log_player_handle(value);
  input.value = saved;
  appendLog(
    saved
      ? `Pseudo RSI enregistré : « ${saved} ». Les événements du Game.log seront filtrés en conséquence.`
      : "Pseudo RSI effacé : tous les événements détectés seront à nouveau annoncés.",
    "info"
  );
}

// Appelé par Python (voir _on_game_event dans app.py) quand le pseudo RSI
// est détecté automatiquement dans le Game.log, uniquement si le champ
// était encore vide.
function gameLogHandleAutoDetected(nickname) {
  const input = document.getElementById("gameLogPlayerHandleInput");
  if (input && !input.value.trim()) {
    input.value = nickname;
  }
  appendLog(`Pseudo RSI détecté automatiquement dans le Game.log : « ${nickname} ».`, "info");
}

// Construit la liste des phrases éditables (une ligne par type d'événement
// du Game.log — voir DEFAULT_GAME_LOG_PHRASES / GAME_LOG_PHRASE_META côté
// app.py). Reconstruit tout le bloc à chaque ouverture des réglages, plutôt
// que de tenter un diff : la liste est courte et ça évite les décalages
// d'écouteurs si jamais les clés changent d'une version à l'autre.
function renderGameLogPhrases(phrases, defaults, meta) {
  const container = document.getElementById("gameLogPhrasesList");
  if (!container) return;
  container.innerHTML = "";

  Object.keys(defaults).forEach((key) => {
    const info = meta[key] || {};
    const label = info.label || key;
    const placeholders = info.placeholders || [];
    const savedValue = phrases[key] || defaults[key] || "";

    const row = document.createElement("div");
    row.className = "game-log-phrase-row";
    row.dataset.savedValue = savedValue;

    const labelRow = document.createElement("div");
    labelRow.className = "game-log-row-label-line";
    const labelEl = document.createElement("label");
    labelEl.setAttribute("for", `gameLogPhrase_${key}`);
    labelEl.textContent = placeholders.length
      ? `${label} — variables : ${placeholders.map((p) => `{${p}}`).join(", ")}`
      : label;
    labelRow.appendChild(labelEl);
    const statusEl = document.createElement("span");
    statusEl.className = "game-log-row-status";
    labelRow.appendChild(statusEl);
    row.appendChild(labelRow);

    const group = document.createElement("div");
    group.className = "input-group";

    const input = document.createElement("input");
    input.type = "text";
    input.id = `gameLogPhrase_${key}`;
    input.autocomplete = "off";
    input.placeholder = defaults[key] || "";
    input.value = savedValue;
    input.addEventListener("input", () => updateGameLogRowStatus(row, input, statusEl));
    group.appendChild(input);

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn btn-ghost btn-sm";
    saveBtn.textContent = "Enregistrer";
    saveBtn.addEventListener("click", () => onSaveGameLogPhrase(key, input));
    group.appendChild(saveBtn);

    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "btn btn-ghost btn-sm";
    resetBtn.textContent = "Réinitialiser";
    resetBtn.addEventListener("click", () => onResetGameLogPhrase(key, input, defaults[key]));
    group.appendChild(resetBtn);

    row.appendChild(group);
    container.appendChild(row);
    updateGameLogRowStatus(row, input, statusEl);
  });
}

// Met à jour l'indicateur visuel d'une ligne (phrase par événement ou
// correction de notification) : ROUGE si le champ a été modifié mais pas
// encore enregistré (la valeur tapée diffère de la dernière valeur
// effectivement sauvegardée, mémorisée dans row.dataset.savedValue), sinon
// ORANGE si la ligne correspond à une entrée nouvellement ajoutée sans
// avoir encore été personnalisée (savedValue === originalValue, ne
// s'applique qu'aux corrections de notifications — voir
// row.dataset.originalValue). Appelée à chaque frappe et juste après
// chaque rendu pour poser l'état initial.
function updateGameLogRowStatus(row, input, statusEl) {
  const saved = row.dataset.savedValue ?? "";
  const original = row.dataset.originalValue;
  const dirty = input.value.trim() !== saved;
  const isNew = !dirty && original !== undefined && saved === original;
  row.classList.toggle("is-unsaved", dirty);
  row.classList.toggle("is-new", isNew);
  if (dirty) {
    statusEl.textContent = "● non enregistré";
    statusEl.className = "game-log-row-status status-unsaved";
  } else if (isNew) {
    statusEl.textContent = "● nouvelle entrée";
    statusEl.className = "game-log-row-status status-new";
  } else {
    statusEl.textContent = "";
    statusEl.className = "game-log-row-status";
  }
}

async function onSaveGameLogPhrase(key, input) {
  const res = await window.pywebview.api.set_game_log_phrase(key, input.value.trim());
  if (res && res.ok) {
    input.value = res.text;
    const row = input.closest(".game-log-phrase-row");
    if (row) {
      row.dataset.savedValue = res.text;
      const statusEl = row.querySelector(".game-log-row-status");
      updateGameLogRowStatus(row, input, statusEl);
    }
    appendLog(`Phrase mise à jour : « ${res.text} »`, "info");
  } else {
    appendLog("Impossible d'enregistrer cette phrase.", "error");
  }
}

async function onResetGameLogPhrase(key, input, defaultText) {
  const res = await window.pywebview.api.reset_game_log_phrase(key);
  if (res && res.ok) {
    const text = (res.phrases && res.phrases[key]) || defaultText || "";
    input.value = text;
    const row = input.closest(".game-log-phrase-row");
    if (row) {
      row.dataset.savedValue = text;
      const statusEl = row.querySelector(".game-log-row-status");
      updateGameLogRowStatus(row, input, statusEl);
    }
    appendLog("Phrase réinitialisée au texte par défaut.", "info");
  }
}

// Liste des corrections de lecture pour des notifications HUD précises
// (voir set_game_log_hud_override côté app.py). Contrairement aux gabarits
// par type d'événement, ici la clé est le texte exact détecté dans le jeu.
function renderGameLogHudOverrides(overrides) {
  const container = document.getElementById("gameLogHudOverridesList");
  if (!container) return;
  container.innerHTML = "";

  const entries = Object.entries(overrides || {});
  if (entries.length === 0) {
    const empty = document.createElement("p");
    empty.className = "ai-setting-desc";
    empty.textContent = "Aucune correction pour l'instant.";
    container.appendChild(empty);
    return;
  }

  entries.forEach(([original, custom]) => {
    const row = document.createElement("div");
    row.className = "game-log-override-row";
    row.dataset.originalValue = original;
    row.dataset.savedValue = custom;

    const headerRow = document.createElement("div");
    headerRow.className = "game-log-row-label-line";
    const originalEl = document.createElement("div");
    originalEl.className = "game-log-override-original";
    originalEl.textContent = `Détecté : « ${original} »`;
    headerRow.appendChild(originalEl);
    const statusEl = document.createElement("span");
    statusEl.className = "game-log-row-status";
    headerRow.appendChild(statusEl);
    row.appendChild(headerRow);

    const group = document.createElement("div");
    group.className = "input-group";

    const input = document.createElement("input");
    input.type = "text";
    input.value = custom;
    input.addEventListener("input", () => updateGameLogRowStatus(row, input, statusEl));
    group.appendChild(input);

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "btn btn-ghost btn-sm";
    saveBtn.textContent = "Enregistrer";
    saveBtn.addEventListener("click", () => onSaveGameLogHudOverride(original, input));
    group.appendChild(saveBtn);

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn btn-ghost btn-sm danger";
    deleteBtn.textContent = "Supprimer";
    deleteBtn.addEventListener("click", () => onDeleteGameLogHudOverride(original));
    group.appendChild(deleteBtn);

    row.appendChild(group);
    container.appendChild(row);
    updateGameLogRowStatus(row, input, statusEl);
  });
}

async function onSaveGameLogHudOverride(original, input) {
  const res = await window.pywebview.api.set_game_log_hud_override(original, input.value.trim());
  if (res && res.ok) {
    state.gameLogHudOverrides = res.overrides || {};
    renderGameLogHudOverrides(state.gameLogHudOverrides);
    appendLog("Correction de lecture mise à jour.", "info");
  }
}

async function onDeleteGameLogHudOverride(original) {
  const res = await window.pywebview.api.delete_game_log_hud_override(original);
  if (res && res.ok) {
    state.gameLogHudOverrides = res.overrides || {};
    renderGameLogHudOverrides(state.gameLogHudOverrides);
    appendLog("Correction de lecture supprimée.", "info");
  }
}

async function onAddGameLogHudOverride() {
  const originalInput = document.getElementById("gameLogHudOverrideOriginalInput");
  const customInput = document.getElementById("gameLogHudOverrideCustomInput");
  const original = originalInput.value.trim();
  const custom = customInput.value.trim();
  if (!original || !custom) {
    appendLog("Renseigne le texte détecté ET le texte à lire avant d'ajouter une correction.", "error");
    return;
  }
  const res = await window.pywebview.api.set_game_log_hud_override(original, custom);
  if (res && res.ok) {
    state.gameLogHudOverrides = res.overrides || {};
    renderGameLogHudOverrides(state.gameLogHudOverrides);
    originalInput.value = "";
    customInput.value = "";
    appendLog("Correction de lecture ajoutée.", "info");
  } else {
    appendLog("Impossible d'ajouter cette correction.", "error");
  }
}

// Appelé par Python (voir _register_hud_text_seen / _on_game_event dans
// app.py) dès qu'une notification HUD JAMAIS VUE auparavant est détectée
// dans le Game.log : l'ajoute immédiatement à la liste des corrections de
// lecture (avec le texte brut comme valeur de départ, donc aucun
// changement de comportement tant qu'elle n'est pas personnalisée),
// visible dès la prochaine ouverture des réglages sans que l'utilisateur
// ait à la copier-coller lui-même. Si les réglages sont déjà ouverts, la
// liste se met à jour en direct.
function gameLogHudOverrideAdded(original, rawText) {
  if (Object.prototype.hasOwnProperty.call(state.gameLogHudOverrides, original)) return;
  state.gameLogHudOverrides[original] = rawText;
  if (document.getElementById("gameLogHudOverridesList")) {
    renderGameLogHudOverrides(state.gameLogHudOverrides);
  }
}

function onPiperSpeedInput(e) {
  document.getElementById("piperSpeedValue").textContent = `${Number(e.target.value).toFixed(2)}×`;
}

async function onPiperSpeedChange(e) {
  await window.pywebview.api.ai_set_piper_length_scale(e.target.value);
}

function onPiperExpressivenessInput(e) {
  document.getElementById("piperExpressivenessValue").textContent = Number(e.target.value).toFixed(2);
}

async function onPiperExpressivenessChange(e) {
  await window.pywebview.api.ai_set_piper_noise_scale(e.target.value);
}

function renderPiperVoices(voices) {
  const list = document.getElementById("piperVoiceList");
  list.innerHTML = "";
  voices.forEach((v) => {
    const row = document.createElement("div");
    row.className = "piper-voice-row" + (v.id === state.piperVoice ? " active" : "");
    row.dataset.voiceId = v.id;

    const actions = v.installed
      ? `
        <button class="btn btn-ghost btn-sm" data-action="select">${v.id === state.piperVoice ? "✓ Active" : "Utiliser"}</button>
        <button class="btn btn-ghost btn-sm" data-action="test">🔊</button>
        <button class="btn btn-ghost btn-sm danger" data-action="delete">🗑</button>
      `
      : `<button class="btn btn-ghost btn-sm" data-action="download">⬇ Télécharger</button>`;

    row.innerHTML = `
      <span class="piper-voice-label">${escapeHtml(v.label)}</span>
      <span class="piper-voice-actions">${actions}</span>
    `;
    list.appendChild(row);
  });

  list.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", onPiperVoiceAction);
  });
}

async function onPiperVoiceAction(e) {
  const btn = e.currentTarget;
  const row = btn.closest(".piper-voice-row");
  const voiceId = row.dataset.voiceId;
  const action = btn.dataset.action;

  if (action === "download") {
    btn.disabled = true;
    btn.textContent = "Préparation...";
    await window.pywebview.api.piper_download_voice(voiceId);
  } else if (action === "select") {
    state.piperVoice = await window.pywebview.api.ai_set_piper_voice(voiceId);
    await refreshPiperStatus();
    appendLog(`Voix Piper active : « ${voiceId} ».`, "info");
  } else if (action === "test") {
    await window.pywebview.api.ai_test_piper_voice(voiceId);
  } else if (action === "delete") {
    const confirmed = confirm("Supprimer cette voix Piper téléchargée ?");
    if (!confirmed) return;
    await window.pywebview.api.piper_delete_voice(voiceId);
    await refreshPiperStatus();
  }
}

// Appelé par Python pendant le téléchargement du moteur Piper
function piperInstallProgress(line) {
  const log = document.getElementById("piperInstallLog");
  log.classList.remove("hidden");
  log.textContent += line + "\n";
  log.scrollTop = log.scrollHeight;
}

// Appelé par Python une fois l'installation du moteur Piper terminée
async function piperInstallDone(success) {
  const btn = document.getElementById("piperInstallBtn");
  btn.disabled = false;
  if (success) {
    await refreshPiperStatus();
  } else {
    appendLog("[Erreur] L'installation de Piper a échoué.", "error");
  }
}

async function onPiperInstallClick() {
  const btn = document.getElementById("piperInstallBtn");
  const log = document.getElementById("piperInstallLog");
  btn.disabled = true;
  log.classList.remove("hidden");
  log.textContent = "Démarrage de l'installation...\n";
  await window.pywebview.api.piper_install();
}

// Appelé par Python pendant le téléchargement d'une voix Piper précise
function piperVoiceProgress(voiceId, message) {
  const row = document.querySelector(`.piper-voice-row[data-voice-id="${voiceId}"]`);
  if (!row) return;
  let progress = row.querySelector(".piper-voice-progress");
  if (!progress) {
    progress = document.createElement("span");
    progress.className = "piper-voice-progress";
    row.appendChild(progress);
  }
  progress.textContent = message;
}

// Appelé par Python une fois le téléchargement d'une voix Piper terminé
async function piperVoiceDone(voiceId, success) {
  if (!success) {
    appendLog(`[Erreur] Le téléchargement de la voix Piper « ${voiceId} » a échoué.`, "error");
  }
  await refreshPiperStatus();
}

function updateAiWakeDot() {
  const dot = document.getElementById("aiWakeDot");
  if (dot) dot.classList.toggle("active", state.listening);
}

function closeAiChat() {
  document.getElementById("aiModal").classList.add("hidden");
}

async function refreshAiStatus(allowAutostart = false) {
  const badge = document.getElementById("aiStatusBadge");
  const setup = document.getElementById("aiSetup");
  const setupText = document.getElementById("aiSetupText");
  const pullBtn = document.getElementById("aiPullBtn");
  const installBtn = document.getElementById("aiInstallOllamaBtn");

  badge.textContent = "vérification...";
  badge.className = "ai-badge checking";

  const status = await window.pywebview.api.ai_check_status(allowAutostart);

  if (!status.running) {
    badge.textContent = "hors ligne";
    badge.className = "ai-badge offline";
    setup.classList.remove("hidden");
    if (status.installed) {
      setupText.innerHTML = allowAutostart
        ? `Ollama est installé mais son service ne répond pas (la tentative de ` +
          `démarrage automatique a échoué). Lance-le manuellement depuis le menu ` +
          `Démarrer, puis clique sur « Revérifier ». S'il vient d'être installé, ` +
          `redémarre aussi cette application.`
        : `Ollama est installé mais ne semble pas lancé actuellement. Clique sur ` +
          `« Revérifier » pour que NovaVox tente de le démarrer automatiquement.`;
      installBtn.classList.add("hidden");
    } else {
      setupText.innerHTML =
        `Ollama n'est pas détecté. Clique sur « Installer Ollama » ci-dessous : il sera ` +
        `téléchargé, installé, puis le modèle IA choisi (${status.model || "..."}) sera ` +
        `téléchargé automatiquement à la suite.`;
      installBtn.classList.remove("hidden");
    }
    pullBtn.classList.add("hidden");
    return;
  }

  installBtn.classList.add("hidden");

  if (!status.modelReady) {
    badge.textContent = `modèle manquant · ${status.model}`;
    badge.className = "ai-badge offline";
    setup.classList.remove("hidden");
    setupText.textContent =
      `Ollama est bien lancé, mais le modèle "${status.model}" n'est pas encore téléchargé (quelques Go, une seule fois).`;
    pullBtn.classList.remove("hidden");
    return;
  }

  badge.textContent = status.model ? `prêt · ${status.model}` : "prêt";
  badge.className = "ai-badge ready";
  setup.classList.add("hidden");
}

async function startAiInstallOllama() {
  const installBtn = document.getElementById("aiInstallOllamaBtn");
  const installLog = document.getElementById("aiInstallOllamaLog");
  installBtn.disabled = true;
  installLog.classList.remove("hidden");
  installLog.textContent = "Démarrage du téléchargement...\n";

  await window.pywebview.api.ai_install_ollama();
}

// Appelé par Python pendant le téléchargement/lancement de l'installeur Ollama
function aiInstallOllamaProgress(line) {
  const installLog = document.getElementById("aiInstallOllamaLog");
  installLog.textContent += line + "\n";
  installLog.scrollTop = installLog.scrollHeight;
}

// Appelé par Python une fois l'installeur Ollama téléchargé et lancé
// (pas une fois qu'Ollama est réellement installé : l'utilisateur doit
// encore suivre les étapes de l'installeur Windows, puis cliquer sur
// « Revérifier »).
function aiInstallOllamaDone(success) {
  document.getElementById("aiInstallOllamaBtn").disabled = false;
  if (!success) {
    appendLog("[Erreur] Le téléchargement/lancement de l'installeur Ollama a échoué.", "error");
  }
}

async function startAiPull() {
  const pullBtn = document.getElementById("aiPullBtn");
  const pullLog = document.getElementById("aiPullLog");
  pullBtn.disabled = true;
  pullLog.classList.remove("hidden");
  pullLog.textContent = "Démarrage du téléchargement...\n";

  await window.pywebview.api.ai_pull_model();
}

// Appelé par Python pendant le téléchargement du modèle
function aiPullProgress(line) {
  const pullLog = document.getElementById("aiPullLog");
  pullLog.classList.remove("hidden");
  pullLog.textContent += line + "\n";
  pullLog.scrollTop = pullLog.scrollHeight;
}

// Appelé par Python une fois le téléchargement terminé
function aiPullDone(success) {
  document.getElementById("aiPullBtn").disabled = false;
  if (success) {
    refreshAiStatus();
  } else {
    appendLog("[Erreur] Le téléchargement du modèle IA a échoué.", "error");
  }
}

function appendAiMessage(role, content, pending) {
  const chat = document.getElementById("aiChat");
  const bubble = document.createElement("div");
  bubble.className = "ai-msg " + (pending ? "pending" : role);
  bubble.textContent = content;
  chat.appendChild(bubble);
  chat.scrollTop = chat.scrollHeight;
  return bubble;
}

let aiPendingBubble = null;

// Appelé par Python dès que "Nova" + une question ont été reconnus
function aiUserMessage(text) {
  appendAiMessage("user", text);
  aiPendingBubble = appendAiMessage("assistant", "…réflexion…", true);

  // Si le panneau est fermé, on l'ouvre pour montrer la conversation.
  const modal = document.getElementById("aiModal");
  if (modal.classList.contains("hidden")) {
    modal.classList.remove("hidden");
    updateAiWakeDot();
    refreshAiStatus();
  }
}

// Appelé par Python quand la réponse de l'IA est prête
function aiReceiveMessage(text) {
  if (aiPendingBubble) {
    aiPendingBubble.classList.remove("pending");
    aiPendingBubble.classList.add("assistant");
    aiPendingBubble.textContent = text;
    aiPendingBubble = null;
  } else {
    appendAiMessage("assistant", text);
  }
}

async function clearAiChat() {
  await window.pywebview.api.ai_clear_history();
  document.getElementById("aiChat").innerHTML = "";
}

/* ------------------------------------------------------------- Utils */

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (s) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[s]));
}