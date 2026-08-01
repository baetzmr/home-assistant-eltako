"""Registriert das fehlende EEP F6-05-02 (AFRISO ASD20 Rauchmelder).

eltakobus kennt F6-05-02 nicht. Da es mechanisch ein RPS-Telegramm ist
(wie F6-02-01), leiten wir davon ab. Allein durch die Klassendefinition
registriert sich das Profil automatisch in EEP, sodass EEP.find('F6-05-02')
funktioniert. Dekodiert wird der Rauchmelder ohnehin über das rohe
Datenbyte in binary_sensor.py.
"""
from eltakobus.eep import F6_02_01


class F6_05_02(F6_02_01):
    """AFRISO ASD20 Rauchmelder. Erbt RPS-Mechanik von F6-02-01."""
    pass