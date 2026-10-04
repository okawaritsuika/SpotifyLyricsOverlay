"""Windows WebView startup policy for automatic readers and explicit login windows."""
import ctypes


def _window_styles():
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetWindowLongPtrW.argtypes=[ctypes.c_void_p,ctypes.c_int]
    user.GetWindowLongPtrW.restype=ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype=ctypes.c_ssize_t
    return user


def prepare_background_window(window,renderer):
    """Keep pywebview's initial Show/Hide out of the taskbar and keyboard focus."""
    if not window.hidden or renderer!='edgechromium':return
    from webview.platforms.edgechromium import EdgeChrome
    initialize=EdgeChrome.__init__
    def initialize_hidden(browser,form,owner,cache_dir):
        if owner is window and window.hidden:
            # Set these before EnsureCoreWebView2Async. Later managed-property
            # changes can recreate its parent HWND and abort initialization.
            form.ShowInTaskbar=False
            form.Opacity=0
        initialize(browser,form,owner,cache_dir)
    # The worker owns one browser. Never edit the installed dependency or
    # apply this policy to an initially visible original/login window.
    EdgeChrome.__init__=initialize_hidden
    def before_show():
        form=window.native;user=_window_styles()
        handle=form.Handle.ToInt64();style=user.GetWindowLongPtrW(handle,-20)
        hidden_style=(style|0x08000000|0x00000080)&~0x00040000
        if style!=hidden_style:user.SetWindowLongPtrW(handle,-20,hidden_style)
        # Preserve readiness without pywebview's unconditional child Focus().
        def quiet_shown(sender,args):window.events.shown.set()
        form.Shown-=form.on_shown
        form.Shown+=quiet_shown
        def restore():
            window.hidden=False;window.focus=True
            form.Shown-=quiet_shown
            form.Shown+=form.on_shown
            form.ShowInTaskbar=True
            form.Opacity=1
            handle=form.Handle.ToInt64();style=user.GetWindowLongPtrW(handle,-20)
            user.SetWindowLongPtrW(handle,-20,(style|0x00040000)&~(0x08000000|0x00000080))
            window._background_restore=None
        window._background_restore=restore
    window.events.before_show+=before_show


def show_original_window(window):
    """An explicit login request restores a normal focusable visible window."""
    restore=vars(window).get('_background_restore')
    if restore:
        from System import Action
        window.native.Invoke(Action(restore))
    window.show()
