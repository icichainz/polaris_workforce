# French glossary

The words Polaris uses in French, in the office dashboard (Django catalogue
`polaris_workforce/locale/fr/LC_MESSAGES/django.po`) and on the foreman's phone
(`polaris_foreman/lib/l10n/app_fr.arb`). Use these terms everywhere so the office and the
phone say the same thing. Register: construction-site French as spoken in West and
Central Africa, formal **vous**, short sentences, imperative for instructions
("Saisissez", "Choisissez").

## People and places

| English | French | Notes |
| --- | --- | --- |
| site | chantier | "All sites" → "Tous les chantiers" |
| site manager | conducteur de travaux | Approves the roll call. Not "chef de chantier" (a different job on French sites). |
| foreman | chef d’équipe | Takes the roll call on the phone. "Backup foreman" → "chef d’équipe suppléant" |
| worker | ouvrier | Plural "ouvriers"; generic masculine, as on site |
| crew | équipe | "Crew today" → "Équipe du jour" |
| trade | métier | Mason → maçon, carpenter → charpentier, labourer → manœuvre, tiler → carreleur |
| admin | administrateur | |
| accountant | comptable | |
| office | bureau | "Office access" → "Accès bureau" |
| paymaster | payeur | Payslip receipt box |

## Attendance

| English | French | Notes |
| --- | --- | --- |
| roll call | appel | "Take roll call" → "Faire l’appel" |
| attendance | présence(s) | "Attendance rate" → "Taux de présence" |
| present | présent | letter **P** |
| absent | absent | letter **A** |
| half day | demi-journée | letter **½**; "½ day" → "½ journée"; "Half" → "Demi" |
| late | retard | letter **R** (English **L**). Status word "Retard", "late after" → "retard après" |
| not recorded | non saisi | |
| day off | jour de repos | "%(days)s off" → "Repos : …" |
| working days | jours ouvrés | |
| public holidays | jours fériés | |
| worker-day | jour-ouvrier | Unit of work in reports ("jours-ouvrier") |
| arrival / departure | arrivée / départ | "In · Out" → "Arr. · Dép." |
| submit / submitted | envoyer / envoyé | What the foreman does with the roll call |
| approve / approved | valider / validé | "Approvals" → "Validations" |
| send back / sent back | renvoyer / renvoyé | Rejected day: "Renvoyé" |
| lock / locked | clôturer / clôturé | Read-only day, sheet or pay period |
| draft | brouillon | |
| correction | correction | A change after submission, always with a reason ("motif") |
| reason | motif | |
| synced | synchronisé | |

The status letter is always shown with its word (never colour alone). The letters come
from translatable strings (`pgettext` context "attendance status letter (…)" in Django,
`statusLetter…` keys in the ARB files), so French shows **P · ½ · R · A**.

## Pay

| English | French | Notes |
| --- | --- | --- |
| pay period | période de paie | |
| pay day | jour de paie | |
| cash sheet | feuille de caisse | |
| pay out / payout | payer / paie en espèces | "Mark paid" → "Marquer payé" |
| payment | paiement | |
| cash issue (office → site) | remise de fonds | "Cash issued" → "Fonds remis"; "hand-over" → "remise" |
| cash reconciliation | rapprochement de caisse | |
| advance (salary advance) | avance (sur salaire) | "Salary advances" → "Avances sur salaire" |
| repayment / repaid | remboursement / remboursé | |
| deduction | retenue | "Deduct per pay" → "Retenue par paie" |
| adjustment | régularisation | Correction of a day already paid, settled on a later pay |
| proof of receipt | preuve de remise | photo, signature, empreinte digitale ("Empreinte" on short buttons) |
| receipt | reçu | Payslip receipt box |
| daily rate / hourly rate | tarif journalier / tarif horaire | "pay rate" → "tarif" |
| pay type | type de rémunération | |
| pay frequency | fréquence de paie | daily → quotidienne, weekly → hebdomadaire, monthly → mensuelle |
| payment method | mode de paiement | cash → espèces, bank transfer → virement bancaire, mobile money → Mobile Money |
| overtime | heures supplémentaires | "Overtime multiplier" → "Majoration des heures supplémentaires" |
| gross / net | brut / net | "Net to pay" → "Net à payer" |
| payslip | bulletin de paie | The PDF title "Pay statement" too |
| labour cost / budget | coût / budget main-d’œuvre | |

## Office

| English | French |
| --- | --- |
| Today | Aujourd’hui |
| Settings | Paramètres |
| Reports | Rapports |
| Audit log | Journal d’audit |
| Users & roles | Utilisateurs et rôles |
| sign in / sign out | se connecter / se déconnecter |
| PIN | code PIN (the phone just says "code") |
| override (a setting) | personnaliser · "valeur du chantier" / "valeur de l’entreprise" |
| company default | valeur par défaut de l’entreprise |

## Typography

- Typographic apostrophe **’** (as the phone app already does), « » not needed.
- A no-break space (U+00A0) before `:` `?` `!` `;`, so they never start a line.
- Dates come from Django's own French month and day names, lowercase as in French:
  "vendredi 30 octobre 2026". The display formats are translatable strings
  (`|date:_("D j M")` in templates, `_("D j M")` in `apps/payroll/labels.py`): French
  replaces `M` ("Oct") with `N` ("oct.", "juin", "sept."), so "D j M" → "D j N" gives
  "ven 30 oct.". Machine formats (`Y-m-d`, `c`) stay literal.
- Weekday checkboxes and calendar headers use their own "weekday" context (Lun, Mar…),
  so they never override Django's lowercase "D" abbreviation used in dates.
- Placeholders (`%(name)s`, `{count}`) and HTML (`<strong>`) are kept as they are.
- Amounts follow the company number format setting in both languages.
