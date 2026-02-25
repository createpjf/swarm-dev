"""CLI dispatcher — lazy-loads command modules on demand."""
from __future__ import annotations


def dispatch_command(args):
    """Route args.cmd to the appropriate cli module, importing only on use."""
    cmd = getattr(args, "cmd", None)
    json_output = getattr(args, "json", False)

    if cmd is None or cmd == "chat":
        from cli.chat import interactive_main
        interactive_main()

    elif cmd == "version":
        from cli.version_cmd import cmd_version
        cmd_version(json_output=json_output)

    elif cmd in ("onboard", "init", "configure"):
        from cli.chat import cmd_init
        cmd_init(
            provider=getattr(args, "provider", ""),
            api_key=getattr(args, "api_key", ""),
            model=getattr(args, "model", ""),
            non_interactive=getattr(args, "non_interactive", False),
            section=getattr(args, "section", ""),
        )

    elif cmd == "config":
        from cli.config_cmd import cmd_config
        cmd_config(action=args.config_action,
                   path=args.config_path or "",
                   value=args.config_value or "",
                   json_output=json_output)

    elif cmd == "run":
        from cli.status_cmd import cmd_run
        cmd_run(args.task)

    elif cmd == "status":
        from cli.status_cmd import cmd_status
        cmd_status(json_output=json_output)

    elif cmd == "scores":
        from cli.status_cmd import cmd_scores
        cmd_scores(json_output=json_output)

    elif cmd == "doctor":
        if getattr(args, "export", False):
            from cli.doctor_cmd import cmd_doctor_export
            cmd_doctor_export()
        else:
            from cli.doctor_cmd import cmd_doctor
            cmd_doctor(repair=args.repair, deep=args.deep, json_output=json_output)

    elif cmd == "security":
        from cli.security_cmd import cmd_security_audit
        cmd_security_audit(deep=getattr(args, "deep", False),
                           fix=getattr(args, "fix", False))

    elif cmd == "export":
        from cli.export_cmd import cmd_export
        cmd_export(args.task_id, fmt=args.format)

    elif cmd == "cron":
        from cli.cron_cmd import cmd_cron
        cmd_cron(action=args.action, name=args.name, act=args.cron_action,
                 payload=args.payload, schedule_type=args.cron_type,
                 schedule=args.schedule, job_id=args.job_id)

    elif cmd == "gateway":
        from cli.gateway_cmd import cmd_gateway
        cmd_gateway(action=args.action, port=args.port,
                    token=args.token, force=args.force)

    elif cmd == "channels":
        if args.action == "pairing":
            from cli.channels_cmd import cmd_channels_pairing
            cmd_channels_pairing(
                action=args.channel or "list",
                code_or_id=getattr(args, "pairing_arg", ""),
                json_output=json_output,
            )
        else:
            from cli.channels_cmd import cmd_channels
            cmd_channels(action=args.action, channel=args.channel,
                         json_output=json_output)

    elif cmd == "chain":
        from cli.chain_cmd import cmd_chain
        cmd_chain(args.action, args.agent_id)

    elif cmd == "workflow":
        if args.wf_cmd == "list":
            from cli.workflow_cmd import cmd_workflows
            cmd_workflows()
        elif args.wf_cmd == "run":
            from cli.workflow_cmd import cmd_workflow_run
            cmd_workflow_run(args.name, args.input)
        else:
            # Print help — parser reference not available here,
            # so fall back to a message
            print("Usage: cleo workflow <list|run> [options]")

    elif cmd == "agents":
        if getattr(args, "agents_cmd", None) in ("create", "add"):
            from cli.agents_cmd import cmd_agents_add
            cmd_agents_add(args.name, template=getattr(args, 'template', None))
        else:
            print("Usage: cleo agents <create|add> <name> [--template ...]")

    elif cmd == "install":
        from cli.install_cmd import cmd_install
        cmd_install(repo=args.repo, target=args.target)

    elif cmd == "uninstall":
        from cli.install_cmd import cmd_uninstall
        cmd_uninstall()

    elif cmd == "update":
        from cli.install_cmd import cmd_update
        cmd_update(branch=args.branch,
                   check_only=getattr(args, "check", False))

    elif cmd == "search":
        from cli.memory_cmd import cmd_search
        cmd_search(query=args.query, collection=args.collection,
                   limit=args.limit, reindex=args.reindex)

    elif cmd == "memory":
        from cli.memory_cmd import cmd_memory
        cmd_memory(action=args.action, query=args.query,
                   agent=args.agent,
                   output=getattr(args, "output", None),
                   fmt=getattr(args, "fmt", "json"))

    elif cmd == "memo":
        from cli.memo_cmd import cmd_memo
        cmd_memo(action=args.action,
                 query=getattr(args, "query", None),
                 agent=getattr(args, "agent", None),
                 memo_type=getattr(args, "memo_type", None),
                 since=getattr(args, "since", None),
                 until=getattr(args, "until", None),
                 min_quality=getattr(args, "min_quality", 0.6),
                 min_score=getattr(args, "min_score", 7),
                 output=getattr(args, "output", None),
                 upload=getattr(args, "upload", False),
                 dry_run=getattr(args, "dry_run", False))

    elif cmd == "evolve":
        if args.action == "confirm":
            from cli.evolve_cmd import cmd_evolve_confirm
            cmd_evolve_confirm(args.agent_id)

    elif cmd == "logs":
        from cli.logs_cmd import cmd_logs
        cmd_logs(follow=args.follow, agent=args.agent,
                 level=args.level, since=args.since, lines=args.lines,
                 export=getattr(args, "export", ""))

    elif cmd == "plugins":
        from cli.plugins_cmd import cmd_plugins
        cmd_plugins(args)

    elif cmd == "completions":
        from cli.completions_cmd import cmd_completions
        cmd_completions(shell=args.shell)

    else:
        # Default: enter interactive chat mode
        from cli.chat import interactive_main
        interactive_main()
