/* ==========================================================================
   QUANTRA TERMINAL — Shared Navigation JS
   --------------------------------------------------------------------------
   Common functions used by the unified q-nav across all pages.
   ========================================================================== */

// --- User Dropdown ---
function toggleUserMenu() {
  var menu = document.getElementById('userMenu');
  var btn = document.getElementById('userMenuBtn');
  if (!menu || !btn) return;
  if (menu.style.display === 'none' || menu.style.display === '') {
    menu.style.display = 'block';
    btn.classList.add('open');
  } else {
    menu.style.display = 'none';
    btn.classList.remove('open');
  }
}

// Close dropdown when clicking outside
document.addEventListener('click', function(event) {
  var dropdown = document.querySelector('.q-nav-user-dropdown');
  var menu = document.getElementById('userMenu');
  var btn = document.getElementById('userMenuBtn');
  if (dropdown && menu && btn && !dropdown.contains(event.target)) {
    menu.style.display = 'none';
    btn.classList.remove('open');
  }
});

// Populate user info in dropdown
function populateUserInfo(username, role) {
  if (!username) return;
  var initials = username.substring(0, 2).toUpperCase();
  var el;
  el = document.getElementById('userInitials');  if (el) el.textContent = initials;
  el = document.getElementById('userInitials2'); if (el) el.textContent = initials;
  el = document.getElementById('userName');      if (el) el.textContent = username;
  el = document.getElementById('userName2');     if (el) el.textContent = username;
  var roleText = role === 'admin' ? 'Administrator' : 'Member';
  el = document.getElementById('userRole');      if (el) el.textContent = roleText;
  var adminLink = document.getElementById('userMenuAdmin');
  if (adminLink && role === 'admin') adminLink.style.display = '';
}



// Toast notification helper
function showToast(message) {
  var toast = document.getElementById('toast');
  if (toast) {
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(function() { toast.classList.remove('show'); }, 3000);
  }
}

// --- Auth Check (shared across pages) ---
(function() {
  fetch('/api/auth/verify', { credentials: 'same-origin' })
    .then(function(r) { 
      if (!r.ok) throw r;
      return r.json(); 
    })
    .then(function(d) {
      if (!d.ok) { window.location.href = '/login'; return; }
      if (d.user) populateUserInfo(d.user, d.role);
    })
    .catch(function() {});
})();
