import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../data/models/models.dart';
import '../../l10n/gen/app_localizations.dart';
import '../../state/providers.dart';
import '../../state/session.dart';
import '../../theme/tokens.dart';
import '../widgets.dart';

/// PIN sign-in (MobileLogin artboard): lockup, EN/FR, foreman card, four
/// dots, 3×4 keypad with C and ⌫; the button is enabled at four digits.
class PinScreen extends ConsumerStatefulWidget {
  const PinScreen({super.key});

  @override
  ConsumerState<PinScreen> createState() => _PinScreenState();
}

class _PinScreenState extends ConsumerState<PinScreen> {
  final _phone = TextEditingController();
  String? _enteredPhone;
  String _pin = '';
  String? _error;
  DateTime? _lockedUntil;
  Timer? _unlock;
  bool _busy = false;

  @override
  void dispose() {
    _phone.dispose();
    _unlock?.cancel();
    super.dispose();
  }

  bool get _locked =>
      _lockedUntil != null && DateTime.now().isBefore(_lockedUntil!);

  void _press(String key) {
    if (_busy || _locked) return;
    setState(() {
      _error = null;
      if (key == 'del') {
        if (_pin.isNotEmpty) _pin = _pin.substring(0, _pin.length - 1);
      } else if (key == 'clr') {
        _pin = '';
      } else if (_pin.length < 4) {
        _pin += key;
      }
    });
  }

  Future<void> _submit(String phone) async {
    if (_pin.length != 4 || _busy || _locked) return;
    final l = context.l10n;
    setState(() => _busy = true);
    try {
      await ref.read(sessionProvider.notifier).signIn(phone, _pin);
    } on ApiException catch (e) {
      String message;
      // The 5th wrong PIN answers 401 with `attempts_left: 0` and
      // `locked_until`; later tries answer 423 with `locked_until`. Both lock.
      final lockedNow = e.statusCode == 423 || e.lockedUntil != null;
      switch (e.statusCode) {
        case 401 when !lockedNow:
          final left = e.attemptsLeft;
          message = left == null ? l.wrongPinNoCount : l.wrongPin(left);
        case 401 || 423:
          final until = DateTime.tryParse(e.lockedUntil ?? '');
          if (until != null) {
            _lockedUntil = until;
            _unlock?.cancel();
            final wait = until.difference(DateTime.now());
            _unlock = Timer(
              wait.isNegative ? Duration.zero : wait,
              () => mounted ? setState(() => _lockedUntil = null) : null,
            );
          }
          message = until == null
              ? l.lockedNoTime
              : l.lockedUntil(timeOf(ref, e.lockedUntil));
        case 429:
          message = l.throttled(e.retryAfterSeconds ?? 60);
        default:
          message = l.signInError(e.code);
      }
      if (mounted) setState(() => _error = message);
    } on NetworkException {
      if (mounted) setState(() => _error = l.signInOffline);
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _pin = '';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final session = ref.watch(sessionProvider);
    final out = session is SignedOut ? session : const SignedOut();
    final remembered = out.remembered;
    final phone = remembered?.phone ?? _enteredPhone;

    return Scaffold(
      backgroundColor: PColors.ink,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  const Lockup(),
                  const Spacer(),
                  SizedBox(
                    height: 40,
                    child: OutlinedButton(
                      key: const Key('language'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(48, 40),
                        foregroundColor: PColors.white,
                        side: const BorderSide(color: PColors.ink700),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(8),
                        ),
                        textStyle: const TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      onPressed: () =>
                          ref.read(localeProvider.notifier).toggle(),
                      child: Text(
                        l.otherLanguage,
                        semanticsLabel: l.switchLanguage,
                      ),
                    ),
                  ),
                ],
              ),
              if (out.expired)
                Padding(
                  padding: const EdgeInsets.only(top: 16),
                  child: Text(
                    l.sessionExpired,
                    style: const TextStyle(color: PColors.amber, fontSize: 14),
                  ),
                ),
              const SizedBox(height: 24),
              Expanded(
                child: phone == null
                    ? _phoneStep(l)
                    : _pinStep(l, phone, remembered),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _phoneStep(AppLocalizations l) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          l.phoneTitle,
          style: const TextStyle(
            color: PColors.white,
            fontSize: 22,
            fontWeight: FontWeight.w800,
          ),
        ),
        const SizedBox(height: 16),
        TextField(
          key: const Key('phone'),
          controller: _phone,
          keyboardType: TextInputType.phone,
          autofocus: true,
          style: const TextStyle(fontSize: 20),
          decoration: InputDecoration(
            labelText: l.phoneLabel,
            hintText: '+228 90 11 22 33',
          ),
          onSubmitted: (_) => _continuePhone(),
        ),
        const Spacer(),
        FilledButton(onPressed: _continuePhone, child: Text(l.continueButton)),
      ],
    );
  }

  void _continuePhone() {
    final v = _phone.text.replaceAll(RegExp(r'[\s\-]'), '');
    if (v.length < 6) return;
    setState(() => _enteredPhone = v);
  }

  Widget _pinStep(
    AppLocalizations l,
    String phone,
    RememberedUser? remembered,
  ) {
    final done = _pin.length == 4;
    const keys = [
      '1',
      '2',
      '3',
      '4',
      '5',
      '6',
      '7',
      '8',
      '9',
      'clr',
      '0',
      'del',
    ];
    final disabled = _busy || _locked;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Column(
          children: [
            InitialsAvatar(
              remembered?.name ?? '#',
              size: 72,
              dark: true,
              fontSize: 24,
            ),
            const SizedBox(height: 8),
            Text(
              remembered?.name ?? phone,
              style: const TextStyle(
                color: PColors.white,
                fontSize: 20,
                fontWeight: FontWeight.w700,
              ),
            ),
            if (remembered?.site != null)
              Text(
                l.foremanAt(remembered!.site!),
                style: const TextStyle(color: PColors.onInkMuted, fontSize: 15),
              ),
            TextButton(
              onPressed: () {
                setState(() {
                  _enteredPhone = null;
                  _pin = '';
                  _error = null;
                });
                ref.read(sessionProvider.notifier).forget();
              },
              style: TextButton.styleFrom(foregroundColor: PColors.amber),
              child: Text(l.notYou),
            ),
          ],
        ),
        const SizedBox(height: 12),
        Text(
          done ? l.pinEntered : l.pinPrompt,
          textAlign: TextAlign.center,
          style: const TextStyle(
            color: PColors.white,
            fontSize: 17,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: 14),
        Semantics(
          label: l.pinDotsLabel(_pin.length),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              for (var i = 0; i < 4; i++)
                Container(
                  key: Key('dot$i'),
                  margin: const EdgeInsets.symmetric(horizontal: 9),
                  width: 18,
                  height: 18,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: PColors.amber, width: 2),
                    color: i < _pin.length ? PColors.amber : Colors.transparent,
                  ),
                ),
            ],
          ),
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(top: 14),
            child: Text(
              _error!,
              key: const Key('pinError'),
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: Color(0xFFFFB4AB),
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        const Spacer(),
        GridView.count(
          crossAxisCount: 3,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          mainAxisSpacing: 12,
          crossAxisSpacing: 12,
          childAspectRatio: 1.7,
          children: [
            for (final k in keys)
              _Key(
                label: k == 'del' ? '⌫' : (k == 'clr' ? 'C' : k),
                semantics: k == 'del'
                    ? l.keyDelete
                    : (k == 'clr' ? l.keyClear : k),
                filled: k != 'del' && k != 'clr',
                onTap: disabled ? null : () => _press(k),
              ),
          ],
        ),
        const SizedBox(height: 16),
        FilledButton(
          key: const Key('signIn'),
          onPressed: done && !disabled ? () => _submit(phone) : null,
          style: FilledButton.styleFrom(
            disabledBackgroundColor: PColors.inkRaised,
            disabledForegroundColor: PColors.onInkDisabled,
          ),
          child: _busy
              ? const SizedBox(
                  width: 22,
                  height: 22,
                  child: CircularProgressIndicator(
                    strokeWidth: 2.5,
                    color: PColors.ink,
                  ),
                )
              : Text(l.signInButton),
        ),
      ],
    );
  }
}

class _Key extends StatelessWidget {
  const _Key({
    required this.label,
    required this.semantics,
    required this.filled,
    required this.onTap,
  });

  final String label;
  final String semantics;
  final bool filled;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => Semantics(
    button: true,
    enabled: onTap != null,
    label: semantics,
    child: Material(
      color: filled ? PColors.inkRaised : Colors.transparent,
      borderRadius: BorderRadius.circular(14),
      child: InkWell(
        key: Key('key-$label'),
        borderRadius: BorderRadius.circular(14),
        onTap: onTap,
        child: Center(
          child: ExcludeSemantics(
            child: Text(
              label,
              style: TextStyle(
                color: onTap == null ? PColors.onInkDisabled : PColors.white,
                fontSize: 26,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ),
      ),
    ),
  );
}
