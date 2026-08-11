// Apply the saved theme before the main stylesheet paints to avoid a flash.
try {
    const theme = localStorage.getItem('ui-theme');
    if (theme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
} catch (_) {
    // The default light theme remains active when storage is unavailable.
}
