from clipbot.chat import parse_line


def test_privmsg_with_tags_and_emotes():
    line = ("@badge-info=;display-name=Some\\sUser;emotes=25:0-4,12-16;tmi-sent-ts=1700000000123 "
            ":someuser!someuser@someuser.tmi.twitch.tv PRIVMSG #chan :Kappa hello Kappa")
    m = parse_line(line)
    assert m.user == "Some User"
    assert m.text == "Kappa hello Kappa"
    assert m.emotes == ["Kappa", "Kappa"]
    assert m.ts == 1700000000.123


def test_privmsg_without_tags():
    m = parse_line(":bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :hi :) there", now=5.0)
    assert m.user == "bob" and m.text == "hi :) there" and m.ts == 5.0


def test_non_privmsg_ignored():
    assert parse_line(":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!") is None
    assert parse_line("@emote-only=0 :tmi.twitch.tv ROOMSTATE #chan") is None
