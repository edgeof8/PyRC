# scripts/default_fun_commands.py
import random
import re
import os  # For os.path.exists, though api.request_data_file_path handles path construction
import logging  # Added for script-specific logging
from typing import TYPE_CHECKING, List, Dict, Any, Optional, Callable
from pyrc_core.scripting.script_base import ScriptBase

if TYPE_CHECKING:
    from pyrc_core.scripting.script_api_handler import ScriptAPIHandler

# Use a logger specific to this script for better traceability
script_logger = logging.getLogger("pyrc.scripts.default_fun_commands")


class FunCommandsScript(ScriptBase):
    def __init__(self, api_handler: "ScriptAPIHandler"):
        super().__init__(api_handler)
        self.slap_items: List[str] = []
        self.eight_ball_answers: List[str] = []

        self.pyfiglet_available = False
        try:
            import pyfiglet  # pyfiglet is an optional import for /ascii

            self.pyfiglet_available = True
            script_logger.info("pyfiglet library found and enabled for /ascii command.")
        except ImportError:
            self.pyfiglet_available = False
            script_logger.info(
                "pyfiglet library not found. /ascii command will be disabled."
            )

    def load(self):
        self.api.log_info("FunCommandsScript loading data...")
        self.slap_items = self.load_list_from_data_file(
            "slap_items.txt", ["a large trout", "a wet noodle", "a rubber chicken"]
        )
        self.eight_ball_answers = self.load_list_from_data_file(
            "magic_eight_ball_answers.txt",
            [
                "It is certain.",
                "It is decidedly so.",
                "Without a doubt.",
                "Yes – definitely.",
                "You may rely on it.",
                "As I see it, yes.",
                "Most likely.",
                "Outlook good.",
                "Yes.",
                "Signs point to yes.",
                "Reply hazy, try again.",
                "Ask again later.",
                "Better not tell you now.",
                "Cannot predict now.",
                "Concentrate and ask again.",
                "Don't count on it.",
                "My reply is no.",
                "My sources say no.",
                "Outlook not so good.",
                "Very doubtful.",
            ],
        )

        if not self.pyfiglet_available:
            self.api.log_warning(
                "/ascii command disabled: pyfiglet library not found. Install with: pip install pyfiglet"
            )

        self.api.register_command(
            "slap",
            self.handle_slap_command,
            "Usage: /slap <nickname> - Slaps <nickname> with a random item.",
        )
        self.api.register_command(
            "8ball",
            self.handle_8ball_command,
            "Usage: /8ball <question> - Asks the Magic 8-Ball a question.",
        )
        self.api.register_command(
            "dice",
            self.handle_dice_command,
            "Usage: /dice <NdN> (e.g., 2d6) - Rolls NdN dice.",
            aliases=["roll"],
        )
        self.api.register_command(
            "rainbow",
            self.handle_rainbow_command,
            "Usage: /rainbow <text> - Sends <text> in rainbow colors.",
        )
        self.api.register_command(
            "reverse",
            self.handle_reverse_command,
            "Usage: /reverse <text> - Sends <text> reversed.",
        )
        self.api.register_command(
            "wave",
            self.handle_wave_command,
            "Usage: /wave <text> - Sends <text> with a wave effect.",
        )
        if self.pyfiglet_available:
            self.api.register_command(
                "ascii",
                self.handle_ascii_command,
                "Usage: /ascii <text> - Converts <text> to ASCII art and sends it.",
            )

        self.api.log_info("FunCommandsScript loaded and commands registered.")

    def handle_slap_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /slap command"""
        parts = self.ensure_command_args(args_str, "slap", 1)
        if not parts:
            return

        target = parts[0]
        item = random.choice(self.slap_items)
        self.api.send_action(target, f"slaps {target} with {item}")

    def handle_8ball_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /8ball command"""
        parts = self.ensure_command_args(args_str, "8ball", 1)
        if not parts:
            return

        question = " ".join(parts)
        answer = random.choice(self.eight_ball_answers)
        self.api.add_message_to_context(
            event_data.get("active_context_name", "Status"),
            f"Question: {question}\nAnswer: {answer}",
            "system",
        )

    def handle_dice_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /dice command"""
        parts = self.ensure_command_args(args_str, "dice", 1)
        if not parts:
            return

        dice_str = parts[0]
        match = re.match(r"(\d+)d(\d+)", dice_str)
        if not match:
            self.api.add_message_to_context(
                event_data.get("active_context_name", "Status"),
                "Invalid dice format. Use NdN (e.g., 2d6)",
                "error",
            )
            return

        num_dice = int(match.group(1))
        sides = int(match.group(2))

        if num_dice < 1 or sides < 1:
            self.api.add_message_to_context(
                event_data.get("active_context_name", "Status"),
                "Number of dice and sides must be positive",
                "error",
            )
            return

        rolls = [random.randint(1, sides) for _ in range(num_dice)]
        total = sum(rolls)
        self.api.add_message_to_context(
            event_data.get("active_context_name", "Status"),
            f"Rolling {num_dice}d{sides}: {rolls} = {total}",
            "system",
        )

    def handle_rainbow_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /rainbow command"""
        parts = self.ensure_command_args(args_str, "rainbow", 1)
        if not parts:
            return

        text = " ".join(parts)
        colors = ["red", "orange", "yellow", "green", "blue", "indigo", "violet"]
        rainbow_text = ""
        for i, char in enumerate(text):
            color = colors[i % len(colors)]
            rainbow_text += f"\x03{color}{char}"
        rainbow_text += "\x03"  # Reset color

        self.api.add_message_to_context(
            event_data.get("active_context_name", "Status"), rainbow_text, "system"
        )

    def handle_reverse_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /reverse command"""
        parts = self.ensure_command_args(args_str, "reverse", 1)
        if not parts:
            return

        text = " ".join(parts)
        reversed_text = text[::-1]
        self.api.add_message_to_context(
            event_data.get("active_context_name", "Status"), reversed_text, "system"
        )

    def handle_wave_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /wave command"""
        parts = self.ensure_command_args(args_str, "wave", 1)
        if not parts:
            return

        text = " ".join(parts)
        wave_text = ""
        for i, char in enumerate(text):
            if i % 2 == 0:
                wave_text += char.upper()
            else:
                wave_text += char.lower()
        self.api.add_message_to_context(
            event_data.get("active_context_name", "Status"), wave_text, "system"
        )

    def handle_ascii_command(self, args_str: str, event_data: Dict[str, Any]):
        """Handle the /ascii command"""
        if not self.pyfiglet_available:
            self.api.add_message_to_context(
                event_data.get("active_context_name", "Status"),
                "ASCII art command requires pyfiglet library. Install with: pip install pyfiglet",
                "error",
            )
            return

        parts = self.ensure_command_args(args_str, "ascii", 1)
        if not parts:
            return

        text = " ".join(parts)
        try:
            import pyfiglet

            ascii_art = pyfiglet.figlet_format(text)
            for line in ascii_art.split("\n"):
                self.api.add_message_to_context(
                    event_data.get("active_context_name", "Status"), line, "system"
                )
        except Exception as e:
            self.api.add_message_to_context(
                event_data.get("active_context_name", "Status"),
                f"Error generating ASCII art: {e}",
                "error",
            )


# Entry point for ScriptManager
def get_script_instance(api_handler: "ScriptAPIHandler"):
    return FunCommandsScript(api_handler)
